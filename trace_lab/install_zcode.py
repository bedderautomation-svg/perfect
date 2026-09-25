"""Extract the pinned official ZCode runtime; no host installation or repackaged CLI."""
import hashlib
import json
import platform
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import urlopen

VERSION = '3.14.3'
AGENT_VERSION = '0.16.9'
BUNDLES = {
    'aarch64': ('arm64', '1539cfcb4b830dfee95d9552fadaad6d84fdbaed3b85be4dc0d2273716e47fe3', 197808),
    'x86_64': ('x64', 'eef10c4b751963eeca7c5be05e92ad9385cb26a073644d5db94c8b27dd1c19eb', 188392),
}


def install():
    arch, digest, offset = BUNDLES[platform.machine()]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / 'ZCode.AppImage'
        url = f'https://cdn-zcode.z.ai/zcode/electron/releases/{VERSION}/linux-{arch}/ZCode-{VERSION}-linux-{arch}.AppImage'
        with urlopen(url, timeout=120) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
        if hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() != digest:
            raise RuntimeError('Official ZCode bundle checksum mismatch')
        extracted = root / 'bundle'
        subprocess.run(['unsquashfs', '-o', str(offset), '-d', str(extracted), str(archive),
                        'resources/glm', 'resources/config/provider', 'resources/tools'], check=True,
                       stdout=subprocess.DEVNULL)
        target = Path('/opt/zcode')
        shutil.copytree(extracted / 'resources/glm', target, dirs_exist_ok=True)
        shutil.copytree(extracted / 'resources/config/provider', target / 'provider', dirs_exist_ok=True)
        shutil.copytree(extracted / 'resources/tools', target / 'tools', dirs_exist_ok=True)
        launcher = Path('/usr/local/bin/zcode')
        launcher.write_text('#!/bin/sh\nexec node /opt/zcode/zcode.cjs "$@"\n')
        launcher.chmod(0o755)
        version = subprocess.check_output([str(launcher), '--version'], text=True).strip()
        if AGENT_VERSION not in version:
            raise RuntimeError('Unexpected bundled ZCode Agent version: ' + version)
        Path('/usr/local/share/trace-lab-zcode-version.json').write_text(json.dumps({
            'desktop_version': VERSION, 'agent_version': AGENT_VERSION,
            'archive_sha256': digest, 'source': url}) + '\n')


if __name__ == '__main__':
    install()
