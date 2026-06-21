#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 阶段二: Ed25519 零信任签名层 (中枢侧)。

设计见 implementation_plan_phase2.md §1/§3:
- 签名输入严格 `cmd|ts|nonce` 三维联合,防篡改 + 防重放。
- 分级私钥: 热私钥(签白名单只读命令) / 根私钥(离线,签高危命令,不受白名单约束)。
- 威胁模型: VPS1 沦陷 → 黑客拿到热私钥,也只能让边缘跑白名单只读命令。

密钥存放 (规避 .env 多行 PEM 解析断裂):
- 密钥以 **base64(PEM)** 单行字符串存 .env:
  EDGE_HOT_PRIV_KEY  中枢热私钥 (仅 vps1 持有)
  EDGE_HOT_PUBKEY    热公钥 (边缘校验热签名)
  EDGE_ROOT_PUBKEY   根公钥 (边缘校验根签名)
- 根私钥绝不落云端,由管理员本地冷保管,签名时临时喂入。
"""
import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


def _b64_wrap(pem_bytes: bytes) -> str:
    """PEM bytes → 单行 base64 字符串 (便于存 .env)。"""
    return base64.b64encode(pem_bytes).decode("ascii")


def _b64_unwrap(val: str) -> bytes:
    """单行 base64 字符串 → PEM bytes。兼容已带换行的裸 PEM (降级处理)。

    自动补齐 base64 padding (===), 容忍 .env 写入时末尾 '=' 被 shell 吃掉
    的常见坑 (base64 公钥常以 '=' 结尾)。"""
    val = val.strip()
    # 补齐到 4 的倍数 (缺失的 padding '=' 还原)
    pad = (-len(val)) % 4
    if pad:
        val = val + ("=" * pad)
    try:
        return base64.b64decode(val, validate=True)
    except Exception:
        # 兼容: 用户直接贴了多行 PEM
        return val.encode("ascii")


def generate_keypair() -> tuple:
    """生成一对 Ed25519 密钥,返回 (priv_pem_b64, pub_pem_b64) 单行字符串。"""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key()
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _b64_wrap(priv_pem), _b64_wrap(pub_pem)


def _load_priv(priv_key_b64_or_pem: str) -> Ed25519PrivateKey:
    pem = _b64_unwrap(priv_key_b64_or_pem)
    # 兼容老版 cryptography (<3.1) 需显式 backend 参数; 新版已废弃该参数但传 None 仍可
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except TypeError:
        from cryptography.hazmat.backends import default_backend
        return serialization.load_pem_private_key(pem, password=None, backend=default_backend())


def _load_pub(pub_key_b64_or_pem: str) -> Ed25519PublicKey:
    pem = _b64_unwrap(pub_key_b64_or_pem)
    try:
        return serialization.load_pem_public_key(pem)
    except TypeError:
        from cryptography.hazmat.backends import default_backend
        return serialization.load_pem_public_key(pem, backend=default_backend())


def sign_message(message: str, priv_key_b64_or_pem: str) -> str:
    """对任意字符串签名,返回 hex 签名。"""
    priv = _load_priv(priv_key_b64_or_pem)
    return priv.sign(message.encode("utf-8")).hex()


def verify_message(message: str, sig_hex: str, pub_key_b64_or_pem: str) -> bool:
    """验签。签名非法/格式错均返回 False (不抛异常给调用方)。"""
    try:
        pub = _load_pub(pub_key_b64_or_pem)
        pub.verify(bytes.fromhex(sig_hex), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign_task(cmd: str, ts: str, nonce: str, priv_key_b64_or_pem: str) -> str:
    """签下发任务。签名输入 = `cmd|ts|nonce` (严格三段,边缘用同样串验)。"""
    return sign_message(f"{cmd}|{ts}|{nonce}", priv_key_b64_or_pem)


def verify_task(cmd: str, ts: str, nonce: str, sig_hex: str, pub_key_b64_or_pem: str) -> bool:
    """验下发任务签名。"""
    return verify_message(f"{cmd}|{ts}|{nonce}", sig_hex, pub_key_b64_or_pem)


def load_keys_from_env() -> dict:
    """从 os.environ 读密钥。中枢侧: 热私钥+两把公钥;边缘侧: 两把公钥。

    边缘探针不持有任何私钥,只校验。"""
    return {
        "hot_priv": os.environ.get("EDGE_HOT_PRIV_KEY", ""),
        "hot_pub": os.environ.get("EDGE_HOT_PUBKEY", ""),
        "root_pub": os.environ.get("EDGE_ROOT_PUBKEY", ""),
    }


def key_fingerprint(pub_key_b64_or_pem: str) -> str:
    """公钥指纹 (SHA256 前16位),用于核对边缘/中枢公钥一致。"""
    import hashlib

    pem = _b64_unwrap(pub_key_b64_or_pem)
    return hashlib.sha256(pem).hexdigest()[:16]
