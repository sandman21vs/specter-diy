"""KEF envelopes: every version Krux writes must open here, and what is sealed
here must be what any AES-GCM would produce.

Runs on CPython with a real AES behind the ucryptolib interface:

    python3 -m unittest discover -s test/tests_native -p "test_kef.py"
"""
import hashlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(HERE, "..", "..", "src"))

try:
    import aes_shim
except ImportError:  # no `cryptography` package
    aes_shim = None
else:
    sys.modules.setdefault("ucryptolib", aes_shim)
    import kef

    # native_support may have stubbed ucryptolib with an AES that does nothing
    kef.cryptolib = aes_shim

PASSWORD = "test key"
ID = b"test ID"
IV = b"OR\xa1\x93l>2q \x9e\x9dd\x05\x9e\xd7\x8e"
ENTROPY = b"5\x0e{1\xa3g'\xc5\xf2\xfcv\xb5\x83\x04f\x8c"
TEXT = b"wpkh([55f8fc5d/84h/1h/0h]tpubDCkpKFzoqyFkNYDZVPd4kFxGZdeGqQEzAbcd/<0;1>/*) " * 3

# Taken from the Krux test suite (tests/test_kef.py, GCM_ENCRYPTED_KEF): the
# twelve words "crush inherit small egg include title slogan mom remain blouse
# boost bonus" under "test key", 100000 iterations.
KRUX_GCM = (
    b"\x07test ID\x14\x00\x00\nOR\xa1\x93l>2q \x9e\x9dd\xbf\xb7vo]]\x8aO"
    b"\x90\x8e\x86\xe784L\x02]\x8f\xedT"
)

# Produced by krux/src/krux/kef.py for every version it defines, 10000
# iterations: ENTROPY for the plain versions, TEXT for the compressed ones.
KRUX_VECTORS = {
    0: "077465737420494400000001186177abecc4ef1a384b51093c26969c5965f9e4f3071943e0598648df2374b0",
    1: "0774657374204944010000014f52a1936c3e3271209e9d64059ed78e596a8a36ba833e3b998b414d4a9b0183f65246eea1a0fac6ef3448f4876d692a",
    5: "077465737420494405000001186177abecc4ef1a384b51093c26969c7318d7",
    6: "077465737420494406000001186177abecc4ef1a384b51093c26969ce4153a10926e32f8b0010cbfadaad6fe",
    7: "0774657374204944070000011cb8d68f1cf83d31254e7cf6b37ed76cb2434aa560e43363add831fc7d51953eb6c8aed285e8b4e3e204b4883c2cd75bed11b5d23cc1cdb1008f1154d3cca40d7b80f081548ef78cf915f334fa449914f828705d064d87d7a5ad7c4b8c0449dd",
    10: "07746573742049440a0000014f52a1936c3e3271209e9d64059ed78e596a8a36ba833e3b998b414d4a9b01830d17896e",
    11: "07746573742049440b0000014f52a1936c3e3271209e9d64059ed78e596a8a36ba833e3b998b414d4a9b01838a2c34483a225da7aba638704a87cd38",
    12: "07746573742049440c0000014f52a1936c3e3271209e9d64059ed78ed403ed8c464676064b54577f66203b2b911b4a81da2d4d9ab079534a57ef9ffdf40636207e49eeee1ca725a154ad8020313e631ccfad9b8456bffb0638abee317b903949fb7e8f07b35ecdcff50556d73480f578de57f5ee3cdf0eb3148e4ae3",
    15: "07746573742049440f0000014f52a1936c3e3271209e9d642e866a6fd18d0b46ad3320afbc495799809c87ea",
    16: "0774657374204944100000014f52a1936c3e3271209e9d6430a7d990a2621ab6127c1e510900e0a20360b8a20bf89d579773e07c6cfa389da9aa9024d83aa2cc56e2870750f1ab3bc15e9f22beaceee7146260621c30e46ebe4ee79daa29a4ca02d7f9fdfc8639991d2bee3e77",
    20: "0774657374204944140000014f52a1936c3e3271209e9d64b2549dbeb172297d6fe87ac65affc6ffdb9dc3ff",
    21: "0774657374204944150000014f52a1936c3e3271209e9d64ac752e41c29d388dd0a74438efb671c4c05e39be02f39366e960453efc30d9eade6f2607d030bc6d1ef326a16a1874d02c45a505c0321666c93b0fd361efb56c22845b7b359f6ccfbb9e5e2487f5c1eaf96a21148e",
}


@unittest.skipIf(aes_shim is None, "needs the 'cryptography' package")
class KEFTest(unittest.TestCase):
    def test_reads_every_krux_version(self):
        self.assertEqual(sorted(KRUX_VECTORS), sorted(kef.VERSIONS))
        for version, envelope in KRUX_VECTORS.items():
            envelope = bytes.fromhex(envelope)
            expected = TEXT if kef.VERSIONS[version][3] else ENTROPY
            self.assertEqual(kef.unwrap(envelope)[:3], (ID, version, 10000))
            self.assertEqual(kef.decrypt(envelope, PASSWORD), expected, version)

    def test_reads_krux_test_suite_vector(self):
        self.assertEqual(kef.unwrap(KRUX_GCM)[:3], (ID, 20, 100000))
        self.assertEqual(kef.decrypt(KRUX_GCM, PASSWORD), ENTROPY)

    def test_wrong_password(self):
        for version, envelope in KRUX_VECTORS.items():
            with self.assertRaises(kef.KEFAuthError, msg=version):
                kef.decrypt(bytes.fromhex(envelope), "not the key")

    def test_writes_what_krux_writes(self):
        self.assertEqual(
            kef.encrypt(ID, PASSWORD, ENTROPY, 10000, IV[:12]).hex(), KRUX_VECTORS[20]
        )
        self.assertEqual(kef.encrypt(ID, PASSWORD, ENTROPY, 100000, IV[:12]), KRUX_GCM)

    def test_matches_an_independent_gcm(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = hashlib.pbkdf2_hmac("sha256", b"hunter2", b"deadbeef", 20000)
        for size in (1, 15, 16, 17, 20, 24, 28, 32, 33, 100):
            plain = os.urandom(size)
            iv = os.urandom(12)
            reference = AESGCM(key).encrypt(iv, plain, None)
            envelope = kef.encrypt("deadbeef", "hunter2", plain, 20000, iv)
            self.assertEqual(
                envelope,
                b"\x08deadbeef\x14\x00\x00\x02" + iv + reference[: size + 4],
            )
            self.assertEqual(kef.decrypt(envelope, "hunter2"), plain)

    def test_random_iv(self):
        rng = type(sys)("rng")
        rng.get_random_bytes = os.urandom
        saved = sys.modules.get("rng")
        sys.modules["rng"] = rng
        try:
            one = kef.encrypt(ID, PASSWORD, ENTROPY, 10000)
            two = kef.encrypt(ID, PASSWORD, ENTROPY, 10000)
        finally:
            if saved is None:
                del sys.modules["rng"]
            else:
                sys.modules["rng"] = saved
        self.assertNotEqual(one, two)
        self.assertEqual(kef.decrypt(one, PASSWORD), ENTROPY)
        self.assertEqual(kef.decrypt(two, PASSWORD), ENTROPY)

    def test_any_flipped_bit_is_refused(self):
        envelope = bytes.fromhex(KRUX_VECTORS[20])
        for index in range(len(envelope)):
            for bit in (0x01, 0x80):
                damaged = bytearray(envelope)
                damaged[index] ^= bit
                with self.assertRaises(kef.KEFError, msg=index):
                    kef.decrypt(bytes(damaged), PASSWORD)

    def test_iteration_bounds(self):
        head = b"\x07test ID\x14"
        body = bytes(12 + 16 + 4)
        for stored, ok in (
            (b"\x00\x00\x00", False),  # zero rounds
            (b"\x00\x00\x01", True),  # 10000
            (b"\x00\x03\xe8", True),  # 1000 * 10000, the ceiling
            (b"\x00\x03\xe9", False),  # 10010000
            (b"\x00\x27\x10", False),  # 10000 * 10000
            (b"\x00\x27\x11", True),  # 10001, stored as is
            (b"\x98\x96\x80", True),  # 10000000, stored as is
            (b"\x98\x96\x81", False),
            (b"\xff\xff\xff", False),
        ):
            self.assertEqual(kef.is_envelope(head + stored + body), ok, stored)
        for iterations in (0, 9999, 10000001):
            with self.assertRaises(kef.KEFError):
                kef.encrypt(ID, PASSWORD, ENTROPY, iterations, IV[:12])

    def test_iterations_round_trip(self):
        for iterations in (10000, 10001, 100000, 123456, 500000):
            envelope = kef.encrypt(ID, PASSWORD, ENTROPY, iterations, IV[:12])
            self.assertEqual(kef.unwrap(envelope)[2], iterations)

    def test_refuses_what_is_not_an_envelope(self):
        good = bytes.fromhex(KRUX_VECTORS[20])
        for data in (
            b"",
            b"\x00",
            good[:5],
            b"\x00" + good[1:],  # empty ID
            b"\xff" + good[1:],  # ID longer than the data
            b"\x07test\x00ID" + good[8:],  # unprintable ID
            good[:8] + b"\x02" + good[9:],  # version nobody defined
            good[:24],  # nothing after the IV
            good[:25],  # one byte short of a tag plus ciphertext
            b"psbt\xff\x01\x00\x00\x00\x01",
            bytes.fromhex(KRUX_VECTORS[10])[:-1],  # CBC out of alignment
        ):
            self.assertFalse(kef.is_envelope(data), data)
            with self.assertRaises(kef.KEFError):
                kef.decrypt(data, PASSWORD)
        self.assertTrue(kef.is_envelope(good))

    def test_refuses_bad_input_to_encrypt(self):
        for id_, password, plain in (
            (b"", PASSWORD, ENTROPY),
            (b"x" * 253, PASSWORD, ENTROPY),
            (b"tab\there", PASSWORD, ENTROPY),
            (ID, "", ENTROPY),
            (ID, PASSWORD, b""),
        ):
            with self.assertRaises(kef.KEFError):
                kef.encrypt(id_, password, plain, 10000, IV[:12])
        with self.assertRaises(kef.KEFError):
            kef.encrypt(ID, PASSWORD, ENTROPY, 10000, IV[:11])


if __name__ == "__main__":
    unittest.main()
