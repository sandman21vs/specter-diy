"""KEF - the Krux Encryption Format.

A versioned envelope: AES-256 under a key stretched from a password with
PBKDF2-HMAC-SHA256, salted by the envelope's own ID.

    [len_id:1] [id:len_id] [version:1] [iterations:3 BE]
    [iv:0|12|16] [ciphertext] [exposed auth:0|3|4]

It is the format Krux and Kern write, so an envelope sealed here opens there
and the other way round. Every version is read; only AES-GCM (version 20) is
written, which is what both of them write by default.

MicroPython's cryptolib offers ECB and CBC only. CTR and GCM are built here on
top of single block ECB encryption, which is all either mode needs.

An envelope is input a stranger may have picked every byte of. unwrap() bounds
every field before it is used, and decrypt() returns nothing it could not
authenticate.
"""
import hashlib

try:
    import ucryptolib as cryptolib
except ImportError:
    import cryptolib

_ECB = 1
_CBC = 2
_CTR = 6
_GCM = 11

AES_BLOCK = 16

# version: (mode, padding, auth, compressed)
#   padding: None - stream mode, False - NUL bytes, True - PKCS#7
#   auth > 0: that many bytes of sha256(version | iv | plaintext | key),
#             in the clear after the ciphertext. For GCM it is the tag instead.
#   auth < 0: that many bytes of sha256(plaintext), encrypted with it
VERSIONS = {
    0: (_ECB, False, -16, False),
    1: (_CBC, False, -16, False),
    5: (_ECB, False, 3, False),
    6: (_ECB, True, -4, False),
    7: (_ECB, True, -4, True),
    10: (_CBC, False, 4, False),
    11: (_CBC, True, -4, False),
    12: (_CBC, True, -4, True),
    15: (_CTR, None, -4, False),
    16: (_CTR, None, -4, True),
    20: (_GCM, None, 4, False),
    21: (_GCM, None, 4, True),
}
IV_LEN = {_ECB: 0, _CBC: 16, _CTR: 12, _GCM: 12}

VERSION_GCM = 20
DEFAULT_ITERATIONS = 100000

# The floor stops a crafted envelope from declaring a trivial work factor and
# skipping the key stretching. The ceiling stops one from parking the device in
# PBKDF2: the three byte field can ask for a hundred million rounds.
MIN_ITERATIONS = 10000
MAX_ITERATIONS = 10000000

# Compressed versions inflate to a size the envelope picks.
MAX_PLAINTEXT = 8192


class KEFError(Exception):
    pass


class KEFAuthError(KEFError):
    """Wrong password, or an envelope that was damaged or tampered with"""


def _key(password, salt, iterations):
    if isinstance(password, str):
        password = password.encode()
    if not password:
        raise KEFError("Empty password")
    return hashlib.pbkdf2_hmac("sha256", password, bytes(salt), iterations, 32)


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _ctr(ecb, nonce, data, counter):
    """AES-CTR with a 12 byte nonce and a 32 bit big endian block counter"""
    out = bytearray()
    for offset in range(0, len(data), AES_BLOCK):
        stream = ecb.encrypt(nonce + counter.to_bytes(4, "big"))
        out += _xor(data[offset : offset + AES_BLOCK], stream)
        counter += 1
    return bytes(out)


def _ghash_mul(x, h):
    """Multiplies in GF(2^128) with the bit order GCM uses"""
    z = 0
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= h
        h = (h >> 1) ^ 0xE1000000000000000000000000000000 if h & 1 else h >> 1
    return z


def _gcm_tag(ecb, nonce, ciphertext):
    """Full 16 byte GCM tag over a ciphertext, with no associated data"""
    h = int.from_bytes(ecb.encrypt(bytes(AES_BLOCK)), "big")
    y = 0
    for offset in range(0, len(ciphertext), AES_BLOCK):
        block = ciphertext[offset : offset + AES_BLOCK]
        block = block + bytes(AES_BLOCK - len(block))
        y = _ghash_mul(y ^ int.from_bytes(block, "big"), h)
    y = _ghash_mul(y ^ (len(ciphertext) * 8), h)
    return _xor(ecb.encrypt(nonce + b"\x00\x00\x00\x01"), y.to_bytes(AES_BLOCK, "big"))


def _same(a, b):
    """Compares without stopping at the first difference"""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


def _inflate(data):
    """Raw deflate with the 1 KB window the format prescribes"""
    try:
        import io

        try:
            import deflate
        except ImportError:
            # CPython, where the tests run
            import zlib

            stream = zlib.decompressobj(-15)
            out = stream.decompress(bytes(data), MAX_PLAINTEXT + 1)
        else:
            with deflate.DeflateIO(io.BytesIO(data), deflate.RAW, 10) as stream:
                out = stream.read(MAX_PLAINTEXT + 1)
    except Exception:
        raise KEFAuthError("Decompression failed")
    if not out or len(out) > MAX_PLAINTEXT:
        raise KEFAuthError("Decompression failed")
    return out


def unwrap(envelope):
    """Parses and bounds an envelope without decrypting anything.

    Returns (id, version, iterations, payload), where payload is everything
    after the header: iv, ciphertext and exposed auth.
    """
    envelope = bytes(envelope)
    if len(envelope) < 6:
        raise KEFError("Not a KEF envelope")
    len_id = envelope[0]
    if len_id == 0 or len(envelope) < len_id + 5:
        raise KEFError("Not a KEF envelope")

    # IDs are labels a person typed. Requiring printable ASCII turns away
    # arbitrary binary that would otherwise pass for a plausible header.
    id_ = envelope[1 : 1 + len_id]
    for char in id_:
        if char < 0x20 or char > 0x7E:
            raise KEFError("Not a KEF envelope")

    version = envelope[1 + len_id]
    if version not in VERSIONS:
        raise KEFError("Unsupported KEF version")
    mode, padding, auth, _ = VERSIONS[version]

    stored = int.from_bytes(envelope[2 + len_id : 5 + len_id], "big")
    iterations = stored * 10000 if stored <= 10000 else stored
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise KEFError("Invalid iteration count")

    payload = envelope[5 + len_id :]
    cipher_len = len(payload) - IV_LEN[mode] - max(auth, 0)
    if padding is None:
        if cipher_len < 1:
            raise KEFError("Envelope too short")
    elif cipher_len < AES_BLOCK or cipher_len % AES_BLOCK:
        raise KEFError("Ciphertext is not aligned")
    return id_, version, iterations, payload


def is_envelope(data):
    """True when data parses as a KEF envelope"""
    try:
        unwrap(data)
    except KEFError:
        return False
    return True


def _auth_matches(version, key, iv, data, expected):
    """Checks the sha256 based auth that ECB, CBC and CTR versions carry"""
    auth = VERSIONS[version][2]
    if auth > 0:
        digest = hashlib.sha256(bytes([version]) + iv + data + key).digest()
    else:
        digest = hashlib.sha256(data).digest()
    return _same(digest[: abs(auth)], expected)


def _unpad_and_authenticate(version, key, iv, decrypted, exposed):
    """Strips padding and authenticates what ECB, CBC or CTR decrypted.

    A hidden auth sits at the tail of the decrypted bytes; an exposed one
    arrived next to the ciphertext and is passed in.
    """
    _, padding, auth, _ = VERSIONS[version]
    size = abs(auth)

    if padding is False:
        # NUL padding is ambiguous: a plaintext or a hidden auth that itself
        # ends in zero bytes loses them to the strip. Hand them back one at a
        # time, up to the size of the auth, until the hash agrees.
        stripped = len(decrypted.rstrip(b"\x00"))
        for nuls in range(size + 1):
            end = stripped + nuls
            if end > len(decrypted):
                break
            if auth > 0:
                data, expected = decrypted[:end], exposed
            elif end < size:
                continue
            else:
                data, expected = decrypted[: end - size], decrypted[end - size : end]
            if _auth_matches(version, key, iv, data, expected):
                return data
        raise KEFAuthError("Authentication failed")

    if padding is True:
        pad = decrypted[-1]
        if not 1 <= pad <= AES_BLOCK or pad > len(decrypted):
            raise KEFAuthError("Authentication failed")
        decrypted = decrypted[:-pad]

    # every PKCS#7 and stream version below GCM hides its auth
    if len(decrypted) < size:
        raise KEFAuthError("Authentication failed")
    data = decrypted[: len(decrypted) - size]
    if not _auth_matches(version, key, iv, data, decrypted[len(decrypted) - size :]):
        raise KEFAuthError("Authentication failed")
    return data


def decrypt(envelope, password):
    """Opens an envelope. Raises KEFAuthError on a wrong password."""
    id_, version, iterations, payload = unwrap(envelope)
    mode, _, auth, compressed = VERSIONS[version]
    key = _key(password, id_, iterations)

    iv = payload[: IV_LEN[mode]]
    payload = payload[IV_LEN[mode] :]
    exposed = b""
    if auth > 0:
        exposed = payload[-auth:]
        payload = payload[:-auth]

    if mode == _GCM:
        ecb = cryptolib.aes(key, _ECB)
        # the tag is checked before a single byte is decrypted
        if not _same(_gcm_tag(ecb, iv, payload)[:auth], exposed):
            raise KEFAuthError("Authentication failed")
        plain = _ctr(ecb, iv, payload, 2)
    else:
        if mode == _CTR:
            decrypted = _ctr(cryptolib.aes(key, _ECB), iv, payload, 0)
        elif mode == _CBC:
            decrypted = cryptolib.aes(key, _CBC, iv).decrypt(payload)
        else:
            decrypted = cryptolib.aes(key, _ECB).decrypt(payload)
        plain = _unpad_and_authenticate(version, key, iv, bytes(decrypted), exposed)

    # Encrypting refuses an empty plaintext, so an empty one here was crafted
    if not plain:
        raise KEFAuthError("Authentication failed")
    if compressed:
        plain = _inflate(plain)
    return plain


def encrypt(id_, password, plaintext, iterations=DEFAULT_ITERATIONS, iv=None):
    """Seals plaintext in an AES-GCM envelope (version 20).

    id_ labels the envelope and salts the key. iv is for test vectors; leave it
    out and a random one is drawn.
    """
    if isinstance(id_, str):
        id_ = id_.encode()
    if not 1 <= len(id_) <= 252:
        raise KEFError("Invalid ID")
    for char in id_:
        if char < 0x20 or char > 0x7E:
            raise KEFError("Invalid ID")
    if not plaintext:
        raise KEFError("Nothing to encrypt")
    # The same window unwrap() enforces, so nothing is written that could not
    # be read back
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise KEFError("Invalid iteration count")

    mode, _, auth, _ = VERSIONS[VERSION_GCM]
    if iv is None:
        import rng

        iv = rng.get_random_bytes(IV_LEN[mode])
    if len(iv) != IV_LEN[mode]:
        raise KEFError("Wrong IV length")

    ecb = cryptolib.aes(_key(password, id_, iterations), _ECB)
    ciphertext = _ctr(ecb, iv, bytes(plaintext), 2)
    tag = _gcm_tag(ecb, iv, ciphertext)[:auth]

    # Round multiples of 10000 are stored divided by it
    stored = iterations
    if iterations % 10000 == 0 and iterations // 10000 <= 10000:
        stored = iterations // 10000
    return b"".join(
        [
            bytes([len(id_)]),
            id_,
            bytes([VERSION_GCM]),
            stored.to_bytes(3, "big"),
            bytes(iv),
            ciphertext,
            tag,
        ]
    )
