"""Platform-bound sealing for the vault master key.

The master key is random 32 bytes. It is never stored in plaintext on supported
hosts:

* Windows: DPAPI (user profile by default, optional machine scope) with a fixed
  application entropy value.
* systemd hosts: ``systemd-creds`` host-bound credentials, when available.
* explicit development fallback: a 0600 key file, only when
  ``ULTRA_RAG_ALLOW_INSECURE_KEYSTORE=1`` is set. It is reported as a weakness,
  never presented as equivalent to the platform protectors.

There is no recovery key by design: if the sealed master key is lost, registered
values cannot be recovered and must be registered again.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from contracts.util import atomic_write_bytes, ensure_dir

APP_ENTROPY = b"ultra-rag/vault/v1"
SEAL_MAGIC = "ultra-rag-sealed-key"
SEAL_VERSION = 1


class KeystoreError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


if os.name == "nt":  # pragma: no cover - exercised on Windows only
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    def _blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def _dpapi_protect(data: bytes, machine: bool) -> bytes:
        in_blob, _keep = _blob(data)
        entropy, _keep2 = _blob(APP_ENTROPY)
        out = _DATA_BLOB()
        flags = 0x1 | (0x4 if machine else 0)  # CRYPTPROTECT_UI_FORBIDDEN, LOCAL_MACHINE
        if not _crypt32.CryptProtectData(
            ctypes.byref(in_blob), "ultra-rag vault key", ctypes.byref(entropy), None, None, flags, ctypes.byref(out)
        ):
            raise KeystoreError(f"DPAPI protect failed (error {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _dpapi_unprotect(data: bytes) -> bytes:
        in_blob, _keep = _blob(data)
        entropy, _keep2 = _blob(APP_ENTROPY)
        out = _DATA_BLOB()
        description = wintypes.LPWSTR()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), ctypes.byref(description), ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out)
        ):
            raise KeystoreError(
                "DPAPI unprotect failed: this key is bound to another Windows user/host or was modified"
            )
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)


def _protector_name() -> str:
    override = os.environ.get("ULTRA_RAG_KEY_PROTECTOR")
    if override:
        return override
    if os.name == "nt":
        return "dpapi"
    if shutil.which("systemd-creds"):
        return "systemd-creds"
    if os.environ.get("ULTRA_RAG_ALLOW_INSECURE_KEYSTORE") == "1":
        return "file-dev"
    raise KeystoreError(
        "no host-bound key protector is available; install systemd-creds or set "
        "ULTRA_RAG_ALLOW_INSECURE_KEYSTORE=1 only for an isolated development store"
    )


def seal_master_key(path: Path, key: bytes) -> str:
    if len(key) != 32:
        raise KeystoreError("master key must be 32 bytes")
    protector = _protector_name()
    if protector == "dpapi":
        if os.name != "nt":
            raise KeystoreError("dpapi is only available on Windows")
        scope = os.environ.get("ULTRA_RAG_DPAPI_SCOPE", "user")
        sealed = _dpapi_protect(key, machine=(scope == "machine"))
        payload = {"magic": SEAL_MAGIC, "version": SEAL_VERSION, "protector": "dpapi", "scope": scope, "blob": _b64(sealed)}
    elif protector == "systemd-creds":
        credential_name = os.environ.get("ULTRA_RAG_SYSTEMD_CRED_NAME", "ultra-rag-vault")
        scope = os.environ.get("ULTRA_RAG_SYSTEMD_CREDS_SCOPE", "user")
        scope_args = ["--user"] if scope == "user" else []
        sealed_path = path.with_suffix(".systemd")
        ensure_dir(sealed_path.parent)
        try:
            subprocess.run(
                ["systemd-creds", *scope_args, "encrypt", f"--name={credential_name}", "-", str(sealed_path)],
                input=key,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise KeystoreError(f"systemd-creds encryption failed: {error}") from error
        try:
            os.chmod(sealed_path, 0o600)
        except OSError:
            pass
        payload = {
            "magic": SEAL_MAGIC,
            "version": SEAL_VERSION,
            "protector": "systemd-creds",
            "scope": scope,
            "name": credential_name,
            "source": sealed_path.name,
        }
    elif protector == "file-dev":
        if os.environ.get("ULTRA_RAG_ALLOW_INSECURE_KEYSTORE") != "1":
            raise KeystoreError("file-dev protector requires ULTRA_RAG_ALLOW_INSECURE_KEYSTORE=1")
        payload = {"magic": SEAL_MAGIC, "version": SEAL_VERSION, "protector": "file-dev", "blob": _b64(key)}
    else:
        raise KeystoreError(f"unknown key protector: {protector}")
    atomic_write_bytes(path, json.dumps(payload, indent=2).encode("utf-8"))
    return protector


def unseal_master_key(path: Path) -> bytes:
    if not path.exists():
        raise KeystoreError(f"sealed master key is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("magic") != SEAL_MAGIC:
        raise KeystoreError("sealed master key has an invalid magic value")
    protector = payload.get("protector")
    if protector == "dpapi":
        return _dpapi_unprotect(_unb64(payload["blob"]))
    if protector == "systemd-creds":
        sealed_path = path.parent / payload["source"]
        scope = payload.get("scope", "user")
        scope_args = ["--user"] if scope == "user" else []
        if scope == "user" and hasattr(os, "geteuid") and sealed_path.exists():
            try:
                owner_uid = sealed_path.stat().st_uid
            except OSError:
                owner_uid = None
            if owner_uid is not None and owner_uid != os.geteuid():
                raise KeystoreError(
                    "this vault is sealed with systemd-creds --user for the user that owns "
                    f"the vault (uid {owner_uid}); run as that user instead of uid {os.geteuid()}"
                )
        try:
            result = subprocess.run(
                ["systemd-creds", *scope_args, "decrypt", f"--name={payload['name']}", str(sealed_path), "-"],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise KeystoreError(f"systemd-creds decryption failed: {error}") from error
        key = result.stdout
        if len(key) != 32:
            raise KeystoreError("systemd-creds returned an unexpected key length")
        return key
    if protector == "file-dev":
        if os.environ.get("ULTRA_RAG_ALLOW_INSECURE_KEYSTORE") != "1":
            raise KeystoreError("file-dev protector requires ULTRA_RAG_ALLOW_INSECURE_KEYSTORE=1")
        return _unb64(payload["blob"])
    raise KeystoreError(f"unknown key protector in sealed file: {protector}")


def protect_master_key(path: Path) -> str:
    ensure_dir(path.parent)
    key = os.urandom(32)
    return seal_master_key(path, key)


def encrypt_value(master_key: bytes, key_id: str, value: str) -> dict[str, str]:
    nonce = os.urandom(12)
    aad = f"ultra-rag/key/{key_id}".encode("utf-8")
    ciphertext = AESGCM(master_key).encrypt(nonce, value.encode("utf-8"), aad)
    return {"nonce": _b64(nonce), "ciphertext": _b64(ciphertext), "aad": aad.decode("utf-8")}


def decrypt_value(master_key: bytes, key_id: str, sealed: dict[str, str]) -> str:
    try:
        nonce = _unb64(sealed["nonce"])
        ciphertext = _unb64(sealed["ciphertext"])
        aad = sealed.get("aad", f"ultra-rag/key/{key_id}").encode("utf-8")
        return AESGCM(master_key).decrypt(nonce, ciphertext, aad).decode("utf-8")
    except Exception as error:
        raise KeystoreError(f"vault entry cannot be decrypted: {type(error).__name__}") from error
