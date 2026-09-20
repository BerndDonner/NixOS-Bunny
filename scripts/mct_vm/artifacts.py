from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ImageArtifacts:
    """Canonical names for every file belonging to one Bunny VM image.

    All lifecycle stages use this class so image naming cannot drift between
    clone, individualization, image preparation, SSD staging and rollout.
    """

    vm: str
    suffix: str = ""

    @property
    def stem(self) -> str:
        return f"{self.vm}{self.suffix}"

    @property
    def qcow2_name(self) -> str:
        return f"{self.stem}.qcow2"

    @property
    def vars_name(self) -> str:
        return f"{self.stem}.OVMF_VARS.fd"

    @property
    def vmdk_name(self) -> str:
        return f"{self.stem}.vmdk"

    @property
    def compressed_name(self) -> str:
        return f"{self.vmdk_name}.zst"

    @property
    def checksum_name(self) -> str:
        return f"{self.compressed_name}.sha256"

    def qcow2(self, base: Path) -> Path:
        return base / self.qcow2_name

    def vars(self, base: Path) -> Path:
        return base / self.vars_name

    def vmdk(self, base: Path) -> Path:
        return base / self.vmdk_name

    def compressed(self, base: Path) -> Path:
        return base / self.compressed_name

    def checksum(self, base: Path) -> Path:
        return base / self.checksum_name


def image_artifacts(vm: str, suffix: str = "") -> ImageArtifacts:
    vm = vm.strip()
    if not vm:
        raise ValueError("VM name must not be empty")
    return ImageArtifacts(vm=vm, suffix=suffix)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _parse_sidecar_line(text: str, *, expected_filename: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("checksum sidecar must contain exactly one non-empty line")

    parts = lines[0].split(maxsplit=1)
    sha = parts[0].lower()
    if not _HEX64_RE.fullmatch(sha):
        raise ValueError("checksum sidecar does not start with a valid SHA256")

    if len(parts) != 2:
        raise ValueError("checksum sidecar must include the image filename")

    filename = parts[1].lstrip("*").strip()
    if filename != expected_filename:
        raise ValueError(
            f"checksum sidecar names {filename!r}, expected {expected_filename!r}"
        )

    return sha


def read_checksum_sidecar(sidecar: Path, *, expected_filename: str) -> str:
    if not sidecar.is_file():
        raise FileNotFoundError(f"Missing checksum sidecar: {sidecar}")
    try:
        text = sidecar.read_text(encoding="ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Checksum sidecar is not ASCII text: {sidecar}") from exc
    try:
        return _parse_sidecar_line(text, expected_filename=expected_filename)
    except ValueError as exc:
        raise ValueError(f"Invalid checksum sidecar {sidecar}: {exc}") from exc


def write_checksum_sidecar(image: Path) -> str:
    if not image.is_file():
        raise FileNotFoundError(f"Cannot checksum missing image: {image}")

    sha = sha256_file(image)
    sidecar = Path(str(image) + ".sha256")
    sidecar.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=sidecar.name + ".",
        suffix=".tmp",
        dir=str(sidecar.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="ascii", newline="\n") as f:
            f.write(f"{sha}  {image.name}\n")
        tmp_path.replace(sidecar)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return sha


def verify_checksum_sidecar(image: Path, sidecar: Path | None = None) -> str:
    if not image.is_file():
        raise FileNotFoundError(f"Missing image: {image}")
    sidecar = sidecar or Path(str(image) + ".sha256")
    expected = read_checksum_sidecar(sidecar, expected_filename=image.name)
    actual = sha256_file(image)
    if actual != expected:
        raise ValueError(
            f"SHA256 mismatch for {image}: sidecar={expected} actual={actual}"
        )
    return expected
