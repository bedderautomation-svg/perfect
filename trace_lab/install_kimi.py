"""Install a checksum-pinned official Kimi Code release (no Python predecessor)."""
import hashlib
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import urllib.request

VERSION = '2.0.2'
SHA256 = {'aarch64': '2965c558fc2bfb7870033712768870e628de1fcdbefc7b8cddd92e9394067877',
          'x86_64': '3c65aa6f983f157212401d0425a1b15f01c9704bc36f6e4a83bb8e91c4c04da9'}


def main():
    machine = platform.machine()
    arch = {'aarch64': 'arm64', 'x86_64': 'x64'}[machine]
    url = f'https://github.com/MoonshotAI/kimi-code/releases/download/%40moonshot-ai/kimi-code%40{VERSION}/kimi-code-linux-{arch}.tar.gz'
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / 'kimi.tar.gz'
        urllib.request.urlretrieve(url, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256[machine]:
            raise RuntimeError('Kimi release checksum mismatch')
        with tarfile.open(archive) as bundle:
            member = bundle.getmember('kimi')
            if not member.isfile():
                raise RuntimeError('Kimi release does not contain the expected binary')
            binary = Path('/usr/local/bin/kimi')
            binary.write_bytes(bundle.extractfile(member).read()); binary.chmod(0o755)
    if subprocess.check_output(['kimi', '--version'], text=True).strip() != VERSION:
        raise RuntimeError('Unexpected Kimi version')


if __name__ == '__main__':
    main()
