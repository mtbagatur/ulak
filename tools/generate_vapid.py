from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64

root = Path(__file__).resolve().parents[1] / ".secrets"
root.mkdir(mode=0o700, exist_ok=True)
key = ec.generate_private_key(ec.SECP256R1())
private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
(root / "vapid_private.pem").write_bytes(private)
(root / "vapid_private.pem").chmod(0o600)
(root / "vapid_public.txt").write_text(base64.urlsafe_b64encode(public).decode().rstrip("=") + "\n")
(root / "vapid_public.txt").chmod(0o600)
print((root / "vapid_public.txt").read_text().strip())
