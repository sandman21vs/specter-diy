"""A real AES behind the ucryptolib interface, for tests that run on CPython.

native_support stubs ucryptolib with an AES that returns its input, which is
enough to import the app but useless for checking a cipher. This one is backed
by the `cryptography` package and covers the two modes MicroPython has.
"""
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MODE_ECB = 1
MODE_CBC = 2


class aes:
    def __init__(self, key, mode, iv=None):
        if mode == MODE_ECB:
            self._cipher = Cipher(algorithms.AES(bytes(key)), modes.ECB())
        elif mode == MODE_CBC:
            self._cipher = Cipher(algorithms.AES(bytes(key)), modes.CBC(bytes(iv)))
        else:
            raise ValueError("mode")

    def encrypt(self, data):
        return self._cipher.encryptor().update(bytes(data))

    def decrypt(self, data):
        return self._cipher.decryptor().update(bytes(data))
