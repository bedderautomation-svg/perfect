"""Install pinned official binaries in an experiment image (never the host)."""
import gzip
import hashlib
import json
import platform
from pathlib import Path
import tarfile
import tempfile
from urllib.request import urlopen

MUSE_VERSION = '1.3.0-R3401.1'
GROK_VERSION = '1.0.40'
AGY_VERSION = '1.2.7'
MUSE_HASHES = {'aarch64': '5e5ea2a3de3a3fabdff8982aec9423d20eaa7dad05df37efb4264356d0d2e223',
               'x86_64': '71b089d055dfe6e4562092bc484896b61bd96fd6ef9fef9da54a14aa174e2a33'}

AGY_HASHES = {'aarch64': 'd39f939ffc80776bfd2dc10db7b9a1a1b58650f08115fa21065c11d10882c71210f368c2e6060f26966d1a33d6b2dd2bc19bfd641062305af6546c51d494511a', 'x86_64': 'fec769d611c4afdf0ae72d38bdb2652c8e2c8e71e4f6de97a27b80dda3c50429160d9e03776a36a59b8857c20e76783c4a49cb0feb8b2f5c3bf925b0cc03bb77'}

def download(url, path):
    with urlopen(url, timeout=120) as response, path.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def install():
    arch = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(platform.machine(), platform.machine())
    if arch not in MUSE_HASHES:
        raise RuntimeError('Unsupported architecture: ' + arch)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        muse = root / 'muse'
        muse_arch = 'aarch64' if arch == 'aarch64' else 'x86'
        download(f'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version={MUSE_VERSION}&file=muse-{muse_arch}-linux', muse)
        if hashlib.sha256(muse.read_bytes()).hexdigest() != MUSE_HASHES[arch]:
            raise RuntimeError('Muse binary checksum mismatch')
        grok = root / 'grok.gz'
        download(f'https://x.ai/cli/grok-{GROK_VERSION}-linux-{arch}.gz', grok)
        (root / 'grok').write_bytes(gzip.decompress(grok.read_bytes()))
        agy_arch = 'arm' if arch == 'aarch64' else 'x64'
        agy_file_arch = 'arm64' if arch == 'aarch64' else 'x64'
        archive = root / 'agy.tar.gz'
        download(f'https://storage.googleapis.com/antigravity-public/antigravity-cli/{AGY_VERSION}-6731160148115456/linux-{agy_arch}/cli_linux_{agy_file_arch}.tar.gz', archive)
        if hashlib.sha512(archive.read_bytes()).hexdigest() != AGY_HASHES[arch]:
            raise RuntimeError('Antigravity archive checksum mismatch')
        with tarfile.open(archive) as bundle:
            members = [m for m in bundle.getmembers() if Path(m.name).name == 'antigravity' and m.isfile()]
            if len(members) != 1:
                raise RuntimeError('Unexpected Antigravity archive')
            (root / 'agy').write_bytes(bundle.extractfile(members[0]).read())
        for name in ('muse', 'grok', 'agy'):
            target = Path('/usr/local/bin') / name
            target.write_bytes((root / name).read_bytes())
            target.chmod(0o755)
        Path('/usr/local/share/trace-lab-harness-versions.json').write_text(json.dumps(
            {'muse': MUSE_VERSION, 'grok': GROK_VERSION, 'antigravity': AGY_VERSION}) + '\n')


if __name__ == '__main__':
    install()
