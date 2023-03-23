import asyncio
import urllib
import re
from decimal import Decimal
from typing import NamedTuple, Optional, Callable, Any, Sequence
from urllib.parse import urlparse

from . import bitcoin
from .logging import Logger
from .util import parse_max_spend, format_satoshis_plain
from .util import get_asyncio_loop, log_exceptions
from .transaction import PartialTxOutput
from .lnurl import decode_lnurl, request_lnurl, callback_lnurl, LNURLError, LNURL6Data
from .bitcoin import COIN, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC
from .lnaddr import lndecode, LnDecodeException, LnInvoiceException
from .lnutil import IncompatibleOrInsaneFeatures

def maybe_extract_lightning_payment_identifier(data: str) -> Optional[str]:
    data = data.strip()  # whitespaces
    data = data.lower()
    if data.startswith(LIGHTNING_URI_SCHEME + ':ln'):
        cut_prefix = LIGHTNING_URI_SCHEME + ':'
        data = data[len(cut_prefix):]
    if data.startswith('ln'):
        return data
    return None

# URL decode
#_ud = re.compile('%([0-9a-hA-H]{2})', re.MULTILINE)
#urldecode = lambda x: _ud.sub(lambda m: chr(int(m.group(1), 16)), x)


# note: when checking against these, use .lower() to support case-insensitivity
BITCOIN_BIP21_URI_SCHEME = 'bitcoin'
LIGHTNING_URI_SCHEME = 'lightning'


class InvalidBitcoinURI(Exception): pass


def parse_bip21_URI(uri: str) -> dict:
    """Raises InvalidBitcoinURI on malformed URI."""

    if not isinstance(uri, str):
        raise InvalidBitcoinURI(f"expected string, not {repr(uri)}")

    if ':' not in uri:
        if not bitcoin.is_address(uri):
            raise InvalidBitcoinURI("Not a bitcoin address")
        return {'address': uri}

    u = urllib.parse.urlparse(uri)
    if u.scheme.lower() != BITCOIN_BIP21_URI_SCHEME:
        raise InvalidBitcoinURI("Not a bitcoin URI")
    address = u.path

    # python for android fails to parse query
    if address.find('?') > 0:
        address, query = u.path.split('?')
        pq = urllib.parse.parse_qs(query)
    else:
        pq = urllib.parse.parse_qs(u.query)

    for k, v in pq.items():
        if len(v) != 1:
            raise InvalidBitcoinURI(f'Duplicate Key: {repr(k)}')

    out = {k: v[0] for k, v in pq.items()}
    if address:
        if not bitcoin.is_address(address):
            raise InvalidBitcoinURI(f"Invalid bitcoin address: {address}")
        out['address'] = address
    if 'amount' in out:
        am = out['amount']
        try:
            m = re.match(r'([0-9.]+)X([0-9])', am)
            if m:
                k = int(m.group(2)) - 8
                amount = Decimal(m.group(1)) * pow(Decimal(10), k)
            else:
                amount = Decimal(am) * COIN
            if amount > TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN:
                raise InvalidBitcoinURI(f"amount is out-of-bounds: {amount!r} BTC")
            out['amount'] = int(amount)
        except Exception as e:
            raise InvalidBitcoinURI(f"failed to parse 'amount' field: {repr(e)}") from e
    if 'message' in out:
        out['message'] = out['message']
        out['memo'] = out['message']
    if 'time' in out:
        try:
            out['time'] = int(out['time'])
        except Exception as e:
            raise InvalidBitcoinURI(f"failed to parse 'time' field: {repr(e)}") from e
    if 'exp' in out:
        try:
            out['exp'] = int(out['exp'])
        except Exception as e:
            raise InvalidBitcoinURI(f"failed to parse 'exp' field: {repr(e)}") from e
    if 'sig' in out:
        try:
            out['sig'] = bitcoin.base_decode(out['sig'], base=58).hex()
        except Exception as e:
            raise InvalidBitcoinURI(f"failed to parse 'sig' field: {repr(e)}") from e
    if 'lightning' in out:
        try:
            lnaddr = lndecode(out['lightning'])
        except LnDecodeException as e:
            raise InvalidBitcoinURI(f"Failed to decode 'lightning' field: {e!r}") from e
        amount_sat = out.get('amount')
        if amount_sat:
            # allow small leeway due to msat precision
            if abs(amount_sat - int(lnaddr.get_amount_sat())) > 1:
                raise InvalidBitcoinURI("Inconsistent lightning field in bip21: amount")
        address = out.get('address')
        ln_fallback_addr = lnaddr.get_fallback_address()
        if address and ln_fallback_addr:
            if ln_fallback_addr != address:
                raise InvalidBitcoinURI("Inconsistent lightning field in bip21: address")

    return out



def create_bip21_uri(addr, amount_sat: Optional[int], message: Optional[str],
                     *, extra_query_params: Optional[dict] = None) -> str:
    if not bitcoin.is_address(addr):
        return ""
    if extra_query_params is None:
        extra_query_params = {}
    query = []
    if amount_sat:
        query.append('amount=%s'%format_satoshis_plain(amount_sat))
    if message:
        query.append('message=%s'%urllib.parse.quote(message))
    for k, v in extra_query_params.items():
        if not isinstance(k, str) or k != urllib.parse.quote(k):
            raise Exception(f"illegal key for URI: {repr(k)}")
        v = urllib.parse.quote(v)
        query.append(f"{k}={v}")
    p = urllib.parse.ParseResult(
        scheme=BITCOIN_BIP21_URI_SCHEME,
        netloc='',
        path=addr,
        params='',
        query='&'.join(query),
        fragment='',
    )
    return str(urllib.parse.urlunparse(p))



def is_uri(data: str) -> bool:
    data = data.lower()
    if (data.startswith(LIGHTNING_URI_SCHEME + ":") or
            data.startswith(BITCOIN_BIP21_URI_SCHEME + ':')):
        return True
    return False



class FailedToParsePaymentIdentifier(Exception):
    pass

class PayToLineError(NamedTuple):
    line_content: str
    exc: Exception
    idx: int = 0  # index of line
    is_multiline: bool = False

RE_ALIAS = r'(.*?)\s*\<([0-9A-Za-z]{1,})\>'
RE_EMAIL = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'

class PaymentIdentifier(Logger):
    """
    Takes:
        * bitcoin addresses or script
        * paytomany csv
        * openalias
        * bip21 URI
        * lightning-URI (containing bolt11 or lnurl)
        * bolt11 invoice
        * lnurl
    """

    def __init__(self, config, contacts, text):
        Logger.__init__(self)
        self.contacts = contacts
        self.config = config
        self.text = text
        self._type = None
        self.error = None    # if set, GUI should show error and stop
        self.warning = None  # if set, GUI should ask user if they want to proceed
        # more than one of those may be set
        self.multiline_outputs = None
        self.bolt11 = None
        self.bip21 = None
        self.spk = None
        #
        self.openalias = None
        self.openalias_data = None
        #
        self.bip70 = None
        self.bip70_data = None
        #
        self.lnurl = None
        self.lnurl_data = None
        # parse without network
        self.parse(text)

    def is_valid(self):
        return bool(self._type)

    def is_lightning(self):
        return self.lnurl or self.bolt11

    def is_multiline(self):
        return bool(self.multiline_outputs)

    def get_error(self) -> str:
        return self.error

    def needs_round_1(self):
        return self.bip70 or self.openalias or self.lnurl

    def needs_round_2(self):
        return self.lnurl and self.lnurl_data

    def needs_round_3(self):
        return self.bip70

    def parse(self, text):
        # parse text, set self._type and self.error
        text = text.strip()
        if not text:
            return
        if outputs:= self._parse_as_multiline(text):
            self._type = 'multiline'
            self.multiline_outputs = outputs
        elif invoice_or_lnurl := maybe_extract_lightning_payment_identifier(text):
            if invoice_or_lnurl.startswith('lnurl'):
                self._type = 'lnurl'
                try:
                    self.lnurl = decode_lnurl(invoice_or_lnurl)
                except Exception as e:
                    self.error = "Error parsing Lightning invoice" + f":\n{e}"
                    return
            else:
                self._type = 'bolt11'
                self.bolt11 = invoice_or_lnurl
        elif text.lower().startswith(BITCOIN_BIP21_URI_SCHEME + ':'):
            try:
                out = parse_bip21_URI(text)
            except InvalidBitcoinURI as e:
                self.error = _("Error parsing URI") + f":\n{e}"
                return
            self._type = 'bip21'
            self.bip21 = out
            self.bip70 = out.get('r')
        elif scriptpubkey := self.parse_output(text):
            self._type = 'spk'
            self.spk = scriptpubkey
        elif re.match(RE_EMAIL, text):
            self._type = 'alias'
            self.openalias = text
        else:
            truncated_text = f"{text[:100]}..." if len(text) > 100 else text
            self.error = FailedToParsePaymentIdentifier(f"Unknown payment identifier:\n{truncated_text}")

    def get_onchain_outputs(self, amount):
        if self.bip70:
            return self.bip70_data.get_outputs()
        elif self.multiline_outputs:
            return self.multiline_outputs
        elif self.spk:
            return [PartialTxOutput(scriptpubkey=self.spk, value=amount)]
        elif self.bip21:
            address = self.bip21.get('address')
            scriptpubkey = self.parse_output(address)
            return [PartialTxOutput(scriptpubkey=scriptpubkey, value=amount)]

        else:
            raise Exception('not onchain')

    def _parse_as_multiline(self, text):
        # filter out empty lines
        lines = text.split('\n')
        lines = [i for i in lines if i]
        is_multiline = len(lines)>1
        outputs = []  # type: List[PartialTxOutput]
        errors = []
        total = 0
        is_max = False
        for i, line in enumerate(lines):
            try:
                output = self.parse_address_and_amount(line)
            except Exception as e:
                errors.append(PayToLineError(
                    idx=i, line_content=line.strip(), exc=e, is_multiline=True))
                continue
            outputs.append(output)
            if parse_max_spend(output.value):
                is_max = True
            else:
                total += output.value
        if is_multiline or outputs:
            self.error = str(errors) if errors else None
        return outputs

    def parse_address_and_amount(self, line) -> 'PartialTxOutput':
        try:
            x, y = line.split(',')
        except ValueError:
            raise Exception("expected two comma-separated values: (address, amount)") from None
        scriptpubkey = self.parse_output(x)
        amount = self.parse_amount(y)
        return PartialTxOutput(scriptpubkey=scriptpubkey, value=amount)

    def parse_output(self, x) -> bytes:
        try:
            address = self.parse_address(x)
            return bytes.fromhex(bitcoin.address_to_script(address))
        except Exception as e:
            error = PayToLineError(idx=0, line_content=x, exc=e, is_multiline=False)
        try:
            script = self.parse_script(x)
            return bytes.fromhex(script)
        except Exception as e:
            #error = PayToLineError(idx=0, line_content=x, exc=e, is_multiline=False)
            pass
        #raise Exception("Invalid address or script.")
        #self.errors.append(error)

    def parse_script(self, x):
        script = ''
        for word in x.split():
            if word[0:3] == 'OP_':
                opcode_int = opcodes[word]
                script += construct_script([opcode_int])
            else:
                bytes.fromhex(word)  # to test it is hex data
                script += construct_script([word])
        return script

    def parse_amount(self, x):
        x = x.strip()
        if not x:
            raise Exception("Amount is empty")
        if parse_max_spend(x):
            return x
        p = pow(10, self.config.get_decimal_point())
        try:
            return int(p * Decimal(x))
        except decimal.InvalidOperation:
            raise Exception("Invalid amount")

    def parse_address(self, line):
        r = line.strip()
        m = re.match('^'+RE_ALIAS+'$', r)
        address = str(m.group(2) if m else r)
        assert bitcoin.is_address(address)
        return address

    def get_fields_for_GUI(self, wallet):
        """ sets self.error as side effect"""
        recipient = None
        amount = None
        description = None
        amount_required = False
        validated = None

        if self.openalias and self.openalias_data:
            address = self.openalias_data.get('address')
            name = self.openalias_data.get('name')
            recipient = self.openalias + ' <' + address + '>'
            validated = self.openalias_data.get('validated')#
            if not validated:
                self.warning = _('WARNING: the alias "{}" could not be validated via an additional '
                                 'security check, DNSSEC, and thus may not be correct.').format(self.openalias)
            #self.payto_e.set_openalias(key=pi.openalias, data=oa_data)
            #self.window.contact_list.update()

        elif self.bolt11:
            recipient, amount, description = self.get_bolt11_fields(self.bolt11)
            if not amount:
                amount_required = True

        elif self.lnurl and self.lnurl_data:
            domain = urlparse(self.lnurl).netloc
            recipient = "invoice from lnurl"
            description = f"lnurl: {domain}: {self.lnurl_data.metadata_plaintext}"
            amount = self.lnurl_data.min_sendable_sat
            amount_required = True

        elif self.bip70 and self.bip70_data:
            pr = self.bip70_data
            if pr.error:
                self.error = pr.error
                return
            recipient = pr.get_requestor()
            amount = pr.get_amount()
            description = pr.get_memo()
            validated = not pr.has_expired()
            #self.set_onchain(True)
            #self.max_button.setEnabled(False)
            # note: allow saving bip70 reqs, as we save them anyway when paying them
            #for btn in [self.send_button, self.clear_button, self.save_button]:
            #    btn.setEnabled(True)
            # signal to set fee
            #self.amount_e.textEdited.emit("")

        elif self.spk:
            amount_required = True

        elif self.multiline_outputs:
            pass

        elif self.bip21:
            recipient = self.bip21.get('address')
            amount = self.bip21.get('amount')
            label = self.bip21.get('label')
            description = self.bip21.get('message')
            # use label as description (not BIP21 compliant)
            if label and not description:
                description = label
            lightning = self.bip21.get('lightning')
            if lightning and wallet.has_lightning():
                # maybe set self.bolt11?
                recipient, amount, description = self.get_bolt11_fields(lightning)
                if not amount:
                    amount_required = True
                # todo: merge logic

        return recipient, amount, description, amount_required, validated

    def get_bolt11_fields(self, bolt11_invoice):
        """Parse ln invoice, and prepare the send tab for it."""
        try:
            lnaddr = lndecode(bolt11_invoice)
        except LnInvoiceException as e:
            self.show_error(_("Error parsing Lightning invoice") + f":\n{e}")
            return
        except IncompatibleOrInsaneFeatures as e:
            self.show_error(_("Invoice requires unknown or incompatible Lightning feature") + f":\n{e!r}")
            return
        pubkey = lnaddr.pubkey.serialize().hex()
        for k,v in lnaddr.tags:
            if k == 'd':
                description = v
                break
        else:
             description = ''
        amount = lnaddr.get_amount_sat()
        return pubkey, amount, description

    async def resolve_openalias(self) -> Optional[dict]:
        key = self.openalias
        if not (('.' in key) and ('<' not in key) and (' ' not in key)):
            return None
        parts = key.split(sep=',')  # assuming single line
        if parts and len(parts) > 0 and bitcoin.is_address(parts[0]):
            return None
        try:
            data = self.contacts.resolve(key)
        except Exception as e:
            self.logger.info(f'error resolving address/alias: {repr(e)}')
            return None
        if data:
            name = data.get('name')
            address = data.get('address')
            self.contacts[key] = ('openalias', name)
            # this will set self.spk
            self.parse(address)
            return data

    def has_expired(self):
        if self.bip70:
            return self.bip70_data.has_expired()
        return False

    @log_exceptions
    async def round_1(self, on_success):
        if self.openalias:
            data = await self.resolve_openalias()
            self.openalias_data = data
            if not self.openalias_data.get('validated'):
                self.warning = _(
                    'WARNING: the alias "{}" could not be validated via an additional '
                    'security check, DNSSEC, and thus may not be correct.').format(self.openalias)
        elif self.bip70:
            from . import paymentrequest
            data = await paymentrequest.get_payment_request(self.bip70)
            self.bip70_data = data
        elif self.lnurl:
            data = await request_lnurl(self.lnurl)
            self.lnurl_data = data
        else:
            return
        on_success(self)

    @log_exceptions
    async def round_2(self, on_success, amount_sat:int=None):
        from .invoices import Invoice
        if self.lnurl:
            if not (self.lnurl_data.min_sendable_sat <= amount_sat <= self.lnurl_data.max_sendable_sat):
                self.error = f'Amount must be between {self._lnurl_data.min_sendable_sat} and {self._lnurl_data.max_sendable_sat} sat.'
                return
            try:
                invoice_data = await callback_lnurl(
                    self.lnurl_data.callback_url,
                    params={'amount': amount_sat * 1000},
                )
            except LNURLError as e:
                self.error = f"LNURL request encountered error: {e}"
                return
            bolt11_invoice = invoice_data.get('pr')
            #
            invoice = Invoice.from_bech32(bolt11_invoice)
            if invoice.get_amount_sat() != amount_sat:
                raise Exception("lnurl returned invoice with wrong amount")
            # this will change what is returned by get_fields_for_GUI
            self.bolt11 = bolt11_invoice

        on_success(self)

    @log_exceptions
    async def round_3(self, tx, refund_address, *, on_success):
        if self.bip70:
            ack_status, ack_msg = await self.bip70.send_payment_and_receive_paymentack(tx.serialize(), refund_address)
            self.logger.info(f"Payment ACK: {ack_status}. Ack message: {ack_msg}")
        on_success(self)

    def get_invoice(self, wallet, amount_sat):
        # fixme: wallet not really needed, only height
        from .invoices import Invoice
        if self.is_lightning():
            invoice_str = self.bolt11
            if not invoice_str:
                return
            invoice = Invoice.from_bech32(invoice_str)
            if invoice.amount_msat is None:
                invoice.amount_msat = int(amount_sat * 1000)
            return invoice
        else:
            outputs = self.get_onchain_outputs(amount_sat)
            message = self.bip21.get('message')
            bip70_data = self.bip70_data if self.bip70 else None
            return wallet.create_invoice(
                outputs=outputs,
                message=message,
                pr=bip70_data,
                URI=self.bip21)
