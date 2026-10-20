# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : password.py

import bcrypt


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode(), salt)
    return hashed.decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


if __name__ == "__main__":
    raw_password = "password123"
    hashed_password = hash_password(raw_password)
    print(f"Hashed Password: {hashed_password}")

    is_valid = verify_password(raw_password, hashed_password)
    print(f"Password is valid: {is_valid}")

    is_valid = verify_password("wrongpassword", hashed_password)
    print(f"Password is valid: {is_valid}")