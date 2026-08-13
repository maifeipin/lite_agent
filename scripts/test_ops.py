import sys


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from skills.ops_self_check import ops_self_check
    print(ops_self_check())
