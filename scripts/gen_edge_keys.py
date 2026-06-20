#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性生成 Edge Sentinel 阶段二 Ed25519 密钥对 (热/根)。

用法:
    python3 scripts/gen_edge_keys.py

输出:
- 热密钥对: 热私钥 → vps1 .env (EDGE_HOT_PRIV_KEY), 热公钥 → 边缘 .env
- 根密钥对: 根私钥 → 管理员本地冷保管 (1Password/U盘, 绝不落云端),
            根公钥 → 边缘 .env (EDGE_ROOT_PUBKEY)

密钥以 base64(PEM) 单行字符串输出,直接贴 .env。
"""
import os
import sys

# 允许 `python scripts/gen_edge_keys.py` 直接跑 (把项目根加入 path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edge_crypto import generate_keypair, key_fingerprint


def main():
    print("=" * 64)
    print("Edge Sentinel 阶段二密钥生成")
    print("=" * 64)

    hot_priv, hot_pub = generate_keypair()
    root_priv, root_pub = generate_keypair()

    print("\n【1】热密钥对 (Online App Key) —— 签白名单只读命令\n")
    print("  ▶ 热私钥 → 仅放 vps1 的 .env (中枢持有,签日常只读命令):")
    print(f"    EDGE_HOT_PRIV_KEY={hot_priv}")
    print(f"    # 指纹(校对用): {key_fingerprint(hot_pub)}\n")
    print("  ▶ 热公钥 → 放 5 个边缘节点 .env:")
    print(f"    EDGE_HOT_PUBKEY={hot_pub}")

    print("\n【2】根密钥对 (Offline Root Key) —— 签高危命令,不受白名单约束\n")
    print("  ⚠️  根私钥绝不落云端! 存到本地 1Password / U盘,只在需要签高危命令时临时取出:")
    print(f"    EDGE_ROOT_PRIV_KEY={root_priv}")
    print(f"    # 指纹(校对用): {key_fingerprint(root_pub)}\n")
    print("  ▶ 根公钥 → 放 5 个边缘节点 .env (边缘用它校验根签名):")
    print(f"    EDGE_ROOT_PUBKEY={root_pub}")

    print("\n" + "=" * 64)
    print("部署检查清单:")
    print("  [ ] vps1 .env 含 EDGE_HOT_PRIV_KEY (热私钥) + EDGE_HOT_PUBKEY + EDGE_ROOT_PUBKEY")
    print("  [ ] 5 边缘节点 .env 含 EDGE_HOT_PUBKEY + EDGE_ROOT_PUBKEY (无私钥)")
    print("  [ ] 根私钥已冷保管,未写入任何服务器")
    print("  [ ] 中枢 hot_pub 指纹 == 边缘 hot_pub 指纹 (防中间人替换公钥)")
    print("=" * 64)


if __name__ == "__main__":
    main()
