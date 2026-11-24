from ..util import encode
import secp256k1
import hashlib, hmac
from io import BytesIO
from rng import get_random_bytes
from ucryptolib import aes
from binascii import hexlify

AES_BLOCK = 16
IV_SIZE = 16
MAC_SIZE = 14
AES_CBC = 2


class SecureError(Exception):
    """
    Raised when the card returned an error over secure channel
    """
    pass

class SecureChannelError(Exception):
    """
    Raised when something went wrong with the
    secure channel (i.e. signature is invalid etc)
    """
    pass

def parse_card_signature(raw):
    """Parses the DER signature the card sends when opening the channel.

    The card encodes r and s on at least 32 bytes each. When the top byte of
    one of them happens to be zero (about 1 handshake in 128) it stays there as
    padding, which strict DER forbids, and libsecp256k1 refuses the signature.
    The channel then failed to open and the card looked absent.

    Accept that padding. The signature is still verified against the card's
    public key by the caller, so this does not weaken the handshake.
    """
    try:
        return secp256k1.ecdsa_signature_parse_der(raw)
    except ValueError:
        pass
    # SEQUENCE { INTEGER r, INTEGER s }, short-form lengths only.
    if len(raw) < 8 or raw[0] != 0x30 or raw[1] != len(raw) - 2 or raw[2] != 0x02:
        raise SecureChannelError("Invalid signature encoding.")
    r_len = raw[3]
    s_tag = 4 + r_len
    if len(raw) < s_tag + 2 or raw[s_tag] != 0x02:
        raise SecureChannelError("Invalid signature encoding.")
    s_len = raw[s_tag + 1]
    if s_tag + 2 + s_len != len(raw):
        raise SecureChannelError("Invalid signature encoding.")
    r = bytes(raw[4:s_tag]).lstrip(b"\x00")
    sig_s = bytes(raw[s_tag + 2:]).lstrip(b"\x00")
    if len(r) > 32 or len(sig_s) > 32:
        raise SecureChannelError("Invalid signature encoding.")
    compact = b"\x00" * (32 - len(r)) + r + b"\x00" * (32 - len(sig_s)) + sig_s
    return secp256k1.ecdsa_signature_parse_compact(compact)


class SecureChannel:
    """
    Class that implements secure communication with the card.
    """

    GET_PUBKEY = b"\xB0\xB2\x00\x00"
    OPEN_EE = b"\xB0\xB5\x00\x00"
    OPEN_SE = b"\xB0\xB4\x00\x00"
    SECURE_MSG = b"\xB0\xB6\x00\x00"
    CLOSE = b"\xB0\xB7\x00\x00"
    SUCCESS = b"\x90\x00"

    def __init__(self, applet):
        """Pass Card or Simulator instance here"""
        self.applet = applet
        self.iv = 0
        self.card_pubkey = None
        self.card_aes_key = None
        self.host_aes_key = None
        self.card_mac_key = None
        self.host_mac_key = None
        self.mode = "es"
        self.is_open = False

    def get_card_pubkey(self):
        """Returns static public key of the card.
        This key doesn't change unless applet is reinstalled.
        """
        sec = self.applet.request(self.GET_PUBKEY)
        self.card_pubkey = secp256k1.ec_pubkey_parse(sec)
        return self.card_pubkey

    def derive_keys(self, shared_secret):
        """Derives keys necessary for encryption and authentication"""
        self.host_aes_key = hashlib.sha256(b"host_aes" + shared_secret).digest()
        self.card_aes_key = hashlib.sha256(b"card_aes" + shared_secret).digest()
        self.host_mac_key = hashlib.sha256(b"host_mac" + shared_secret).digest()
        self.card_mac_key = hashlib.sha256(b"card_mac" + shared_secret).digest()
        return hashlib.sha256(shared_secret).digest()[:4]

    def open(self, mode=None):
        """Opens a secure channel.
        Mode can be "es" - ephemeral-static
                 or "ee" - ephemeral-ephemenral
        """
        # save mode for later - i.e. reestablish secure channel
        if mode is None:
            mode = self.mode
        else:
            self.mode = mode
        # check if we know pubkey already
        if self.card_pubkey is None:
            self.get_card_pubkey()
        # generate ephimerial key
        secret = get_random_bytes(32)
        host_prv = secret
        host_pub = secp256k1.ec_pubkey_create(secret)
        # ee mode - ask card to create ephimerial key and send it to us
        if mode == "ee":
            data = secp256k1.ec_pubkey_serialize(host_pub, secp256k1.EC_UNCOMPRESSED)
            # get ephimerial pubkey from the card
            res = self.applet.request(self.OPEN_EE + encode(data))
            s = BytesIO(res)
            data = s.read(65)
            pub = secp256k1.ec_pubkey_parse(data)
            secp256k1.ec_pubkey_tweak_mul(pub, secret)
            shared_secret = hashlib.sha256(
                secp256k1.ec_pubkey_serialize(pub)[1:33]
            ).digest()
            shared_fingerprint = self.derive_keys(shared_secret)
            recv_hmac = s.read(MAC_SIZE)
            h = hmac.new(self.card_mac_key, digestmod="sha256")
            h.update(data)
            expected_hmac = h.digest()[:MAC_SIZE]
            if expected_hmac != recv_hmac:
                raise SecureChannelError("Wrong HMAC.")
            data += recv_hmac
            raw_sig = s.read()
            sig = parse_card_signature(raw_sig)
            # in case card doesn't follow low s rule (but it should)
            sig = secp256k1.ecdsa_signature_normalize(sig)
            if not secp256k1.ecdsa_verify(
                sig, hashlib.sha256(data).digest(), self.card_pubkey
            ):
                raise SecureChannelError("Signature is invalid.")
        # se mode - use our ephimerial key with card's static key
        else:
            data = secp256k1.ec_pubkey_serialize(host_pub, secp256k1.EC_UNCOMPRESSED)
            # ugly copy
            pub = secp256k1.ec_pubkey_parse(
                secp256k1.ec_pubkey_serialize(self.card_pubkey)
            )
            secp256k1.ec_pubkey_tweak_mul(pub, secret)
            shared_secret = secp256k1.ec_pubkey_serialize(pub)[1:33]
            res = self.applet.request(self.OPEN_SE + encode(data))
            s = BytesIO(res)
            nonce_card = s.read(32)
            recv_hmac = s.read(MAC_SIZE)
            secret_with_nonces = hashlib.sha256(shared_secret + nonce_card).digest()
            shared_fingerprint = self.derive_keys(secret_with_nonces)
            data = nonce_card
            h = hmac.new(self.card_mac_key, digestmod="sha256")
            h.update(data)
            expected_hmac = h.digest()[:MAC_SIZE]
            if expected_hmac != recv_hmac:
                raise SecureChannelError("Wrong HMAC.")
            data += recv_hmac
            sig = parse_card_signature(s.read())
            # in case card doesn't follow low s rule (but it should)
            sig = secp256k1.ecdsa_signature_normalize(sig)
            if not secp256k1.ecdsa_verify(
                sig, hashlib.sha256(data).digest(), self.card_pubkey
            ):
                raise SecureChannelError("Signature is invalid")
        # reset iv
        self.iv = 0
        self.is_open = True

    def encrypt(self, data):
        """Encrypts the message for transmission"""
        # add padding
        d = data + b"\x80"
        if len(d) % AES_BLOCK != 0:
            d += b"\x00" * (AES_BLOCK - (len(d) % AES_BLOCK))
        iv = self.iv.to_bytes(IV_SIZE, "big")
        crypto = aes(self.host_aes_key, AES_CBC, iv)
        ct = crypto.encrypt(d)
        h = hmac.new(self.host_mac_key, digestmod="sha256")
        h.update(iv)
        h.update(ct)
        ct += h.digest()[:MAC_SIZE]
        return ct

    def decrypt(self, ct):
        """Decrypts the message received from the card"""
        recv_hmac = ct[-MAC_SIZE:]
        ct = ct[:-MAC_SIZE]
        iv = self.iv.to_bytes(IV_SIZE, "big")
        h = hmac.new(self.card_mac_key, digestmod="sha256")
        h.update(iv)
        h.update(ct)
        expected_hmac = h.digest()[:MAC_SIZE]
        if expected_hmac != recv_hmac:
            raise SecureChannelError("Wrong HMAC.")
        crypto = aes(self.card_aes_key, AES_CBC, iv)
        plain = crypto.decrypt(ct)
        # check and remove \x80... padding
        arr = plain.split(b"\x80")
        if len(arr) == 1 or len(arr[-1].replace(b"\x00", b"")) > 0:
            raise SecureChannelError("Wrong padding")
        return b"\x80".join(arr[:-1])

    def request(self, data):
        """Sends a secure request to the card
        and returns decrypted result.
        Raises a SecureError if errorcode returned from the card.
        """
        # if counter reached maximum - reestablish channel
        if self.iv >= 2 ** 16 or not self.is_open:
            self.open()
        ct = self.encrypt(data)
        res = self.applet.request(self.SECURE_MSG + encode(ct))
        plaintext = self.decrypt(res)
        self.iv += 1
        if plaintext[:2] == self.SUCCESS:
            return plaintext[2:]
        else:
            raise SecureError(hexlify(plaintext[:2]).decode())

    def close(self):
        """Closes the secure channel"""
        self.applet.request(self.CLOSE)
        self.is_open = False
        self.card_pubkey = None
