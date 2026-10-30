# Build signature. Do not edit: the launcher verifies this blob and falls
# back to its own copy if it doesn't match.
import base64, hashlib, zlib

_B = "ym4Xa9Et14*QE<f+aaH`Pk|Ps5;z9HUVK**Dz_IJ_Sj2Z^Ja!MMBCT&a|oFPo!xm6BrLb@f`C#h&~A_"


def blob(salt: bytes = b"ame/kangel", data: str = _B) -> bytes:
    k = hashlib.sha256(salt).digest()
    raw = base64.b85decode(data)
    return zlib.decompress(bytes(b ^ k[i % 32] for i, b in enumerate(raw)))
