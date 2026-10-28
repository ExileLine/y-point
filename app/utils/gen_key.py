# -*- coding: utf-8 -*-
# @Time    : 2026/10/22 14:10
# @Author  : yangyuexiong
# @File    : gen_key.py

from cryptography.fernet import Fernet
import secrets

if __name__ == "__main__":
    #  CREDENTIAL_ENCRYPTION_KEY
    print(secrets.token_urlsafe(48))

    #  SECRET_KEY
    print(Fernet.generate_key().decode())
