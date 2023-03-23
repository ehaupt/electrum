import threading
from typing import TYPE_CHECKING, Optional
import asyncio
from urllib.parse import urlparse

from PyQt5.QtCore import pyqtProperty, pyqtSignal, pyqtSlot, QObject, Q_ENUMS, QTimer

from electrum import bitcoin
from electrum import lnutil
from electrum.i18n import _
from electrum.invoices import Invoice
from electrum.invoices import (PR_UNPAID, PR_EXPIRED, PR_UNKNOWN, PR_PAID, PR_INFLIGHT,
                               PR_FAILED, PR_ROUTING, PR_UNCONFIRMED, LN_EXPIRY_NEVER)
from electrum.lnaddr import LnInvoiceException
from electrum.logging import get_logger
from electrum.transaction import PartialTxOutput
from electrum.util import InvoiceError
from electrum.payment_identifier import PaymentIdentifier, InvalidBitcoinURI, maybe_extract_lightning_payment_identifier
from electrum.lnutil import format_short_channel_id
from electrum.lnurl import decode_lnurl, request_lnurl, callback_lnurl
from electrum.bitcoin import COIN
from electrum.paymentrequest import PaymentRequest

from .qetypes import QEAmount
from .qewallet import QEWallet
from .util import status_update_timer_interval


class QEInvoice(QObject):
    class Type:
        Invalid = -1
        OnchainInvoice = 0
        LightningInvoice = 1
        LNURLPayRequest = 2

    class Status:
        Unpaid = PR_UNPAID
        Expired = PR_EXPIRED
        Unknown = PR_UNKNOWN
        Paid = PR_PAID
        Inflight = PR_INFLIGHT
        Failed = PR_FAILED
        Routing = PR_ROUTING
        Unconfirmed = PR_UNCONFIRMED

    Q_ENUMS(Type)
    Q_ENUMS(Status)

    _logger = get_logger(__name__)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._wallet = None  # type: Optional[QEWallet]
        self._canSave = False
        self._canPay = False
        self._key = None

    walletChanged = pyqtSignal()
    @pyqtProperty(QEWallet, notify=walletChanged)
    def wallet(self):
        return self._wallet

    @wallet.setter
    def wallet(self, wallet: QEWallet):
        if self._wallet != wallet:
            self._wallet = wallet
            self.walletChanged.emit()

    canSaveChanged = pyqtSignal()
    @pyqtProperty(bool, notify=canSaveChanged)
    def canSave(self):
        return self._canSave

    @canSave.setter
    def canSave(self, canSave):
        if self._canSave != canSave:
            self._canSave = canSave
            self.canSaveChanged.emit()

    canPayChanged = pyqtSignal()
    @pyqtProperty(bool, notify=canPayChanged)
    def canPay(self):
        return self._canPay

    @canPay.setter
    def canPay(self, canPay):
        if self._canPay != canPay:
            self._canPay = canPay
            self.canPayChanged.emit()

    keyChanged = pyqtSignal()
    @pyqtProperty(str, notify=keyChanged)
    def key(self):
        return self._key

    @key.setter
    def key(self, key):
        if self._key != key:
            self._key = key
            self.keyChanged.emit()

    userinfoChanged = pyqtSignal()
    @pyqtProperty(str, notify=userinfoChanged)
    def userinfo(self):
        return self._userinfo

    @userinfo.setter
    def userinfo(self, userinfo):
        if self._userinfo != userinfo:
            self._userinfo = userinfo
            self.userinfoChanged.emit()

    def get_max_spendable_onchain(self):
        spendable = self._wallet.confirmedBalance.satsInt
        if not self._wallet.wallet.config.get('confirmed_only', False):
            spendable += self._wallet.unconfirmedBalance.satsInt
        return spendable

    def get_max_spendable_lightning(self):
        return self._wallet.wallet.lnworker.num_sats_can_send() if self._wallet.wallet.lnworker else 0

class QEInvoiceParser(QEInvoice):
    _logger = get_logger(__name__)

    invoiceChanged = pyqtSignal()
    invoiceSaved = pyqtSignal([str], arguments=['key'])

    validationSuccess = pyqtSignal()
    validationWarning = pyqtSignal([str,str], arguments=['code', 'message'])
    validationError = pyqtSignal([str,str], arguments=['code', 'message'])

    invoiceCreateError = pyqtSignal([str,str], arguments=['code', 'message'])


    amountOverrideChanged = pyqtSignal()

    _round_1_signal = pyqtSignal(object)
    _round_2_signal = pyqtSignal(object)
    _round_3_signal = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._invoiceType = QEInvoice.Type.Invalid
        self._recipient = ''
        self._effectiveInvoice = None
        self._amount = QEAmount()
        self._userinfo = ''
        self._lnprops = {}

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.updateStatusString)

        self._amountOverride = QEAmount()
        self._amountOverride.valueChanged.connect(self._on_amountoverride_value_changed)

        self._round_1_signal.connect(self._on_round_1)
        self._round_2_signal.connect(self._on_round_2)
        self._round_3_signal.connect(self._on_round_3)

        self.clear()

    @pyqtProperty(int, notify=invoiceChanged)
    def invoiceType(self):
        return self._invoiceType

    # not a qt setter, don't let outside set state
    def setInvoiceType(self, invoiceType: QEInvoice.Type):
        self._invoiceType = invoiceType

    recipientChanged = pyqtSignal()
    @pyqtProperty(str, notify=recipientChanged)
    def recipient(self):
        return self._recipient

    @recipient.setter
    def recipient(self, recipient: str):
        #if self._recipient != recipient:
        self.canPay = False
        self._recipient = recipient
        self._lnurlData = None
        self.amountOverride = QEAmount()
        if recipient:
            self.validateRecipient(recipient)
        self.recipientChanged.emit()

    @pyqtProperty(str, notify=invoiceChanged)
    def message(self):
        return self._effectiveInvoice.message if self._effectiveInvoice else ''

    @pyqtProperty(QEAmount, notify=invoiceChanged)
    def amount(self):
        if not self._effectiveInvoice:
            self._amount.clear()
            return self._amount
        self._amount.copyFrom(QEAmount(from_invoice=self._effectiveInvoice))
        return self._amount

    @amount.setter
    def amount(self, new_amount):
        self._logger.debug(f'set new amount {repr(new_amount)}')
        if self._effectiveInvoice:
            self._effectiveInvoice.amount_msat = '!' if new_amount.isMax else int(new_amount.satsInt * 1000)

        self.determine_can_pay()
        self.invoiceChanged.emit()

    @pyqtProperty(QEAmount, notify=amountOverrideChanged)
    def amountOverride(self):
        return self._amountOverride

    @amountOverride.setter
    def amountOverride(self, new_amount):
        self._logger.debug(f'set new override amount {repr(new_amount)}')
        self._amountOverride.copyFrom(new_amount)

        self.determine_can_pay()
        self.amountOverrideChanged.emit()

    @pyqtSlot()
    def _on_amountoverride_value_changed(self):
        self.determine_can_pay()

    @pyqtProperty('quint64', notify=invoiceChanged)
    def time(self):
        return self._effectiveInvoice.time if self._effectiveInvoice else 0

    @pyqtProperty('quint64', notify=invoiceChanged)
    def expiration(self):
        return self._effectiveInvoice.exp if self._effectiveInvoice else 0

    @pyqtProperty('quint64', notify=invoiceChanged)
    def time(self):
        return self._effectiveInvoice.time if self._effectiveInvoice else 0

    statusChanged = pyqtSignal()
    @pyqtProperty(int, notify=statusChanged)
    def status(self):
        if not self._effectiveInvoice:
            return PR_UNKNOWN
        return self._wallet.wallet.get_invoice_status(self._effectiveInvoice)

    @pyqtProperty(str, notify=statusChanged)
    def status_str(self):
        if not self._effectiveInvoice:
            return ''
        status = self._wallet.wallet.get_invoice_status(self._effectiveInvoice)
        return self._effectiveInvoice.get_status_str(status)

    @pyqtProperty(str, notify=invoiceChanged)
    def address(self):
        return self._effectiveInvoice.get_address() if self._effectiveInvoice else ''

    @pyqtProperty('QVariantMap', notify=invoiceChanged)
    def lnprops(self):
        return self._lnprops

    def set_lnprops(self):
        self._lnprops = {}
        if not self.invoiceType == QEInvoice.Type.LightningInvoice:
            return

        lnaddr = self._effectiveInvoice._lnaddr
        ln_routing_info = lnaddr.get_routing_info('r')
        self._logger.debug(str(ln_routing_info))

        self._lnprops = {
            'pubkey': lnaddr.pubkey.serialize().hex(),
            'payment_hash': lnaddr.paymenthash.hex(),
            'r': [{
                'node': self.name_for_node_id(x[-1][0]),
                'scid': format_short_channel_id(x[-1][1])
                } for x in ln_routing_info] if ln_routing_info else []
        }

    def name_for_node_id(self, node_id):
        node_alias = self._wallet.wallet.lnworker.get_node_alias(node_id) or node_id.hex()
        return node_alias

    @pyqtSlot()
    def clear(self):
        self.recipient = ''
        self.setInvoiceType(QEInvoice.Type.Invalid)
        self._bip21 = None
        self._lnurlData = None
        self.canSave = False
        self.canPay = False
        self.userinfo = ''
        self.invoiceChanged.emit()

    # don't parse the recipient string, but init qeinvoice from an invoice key
    # this should not emit validation signals
    @pyqtSlot(str)
    def initFromKey(self, key):
        self.clear()
        invoice = self._wallet.wallet.get_invoice(key)
        self._logger.debug(repr(invoice))
        if invoice:
            self.set_effective_invoice(invoice)
            self.key = key

    def set_effective_invoice(self, invoice: Invoice):
        self._effectiveInvoice = invoice

        if invoice.is_lightning():
            self.setInvoiceType(QEInvoice.Type.LightningInvoice)
        else:
            self.setInvoiceType(QEInvoice.Type.OnchainInvoice)

        self.set_lnprops()

        self.determine_can_pay()

        self.invoiceChanged.emit()
        self.statusChanged.emit()

        self.set_status_timer()

    def set_status_timer(self):
        if self.status != PR_EXPIRED:
            if self.expiration > 0 and self.expiration != LN_EXPIRY_NEVER:
                interval = status_update_timer_interval(self.time + self.expiration)
                if interval > 0:
                    self._timer.setInterval(interval)  # msec
                    self._timer.start()
        else:
            self.determine_can_pay() # status went to PR_EXPIRED

    @pyqtSlot()
    def updateStatusString(self):
        self.statusChanged.emit()
        self.set_status_timer()

    def determine_can_pay(self):
        self.canPay = False
        self.canSave = False
        self.userinfo = ''

        if not self.amountOverride.isEmpty:
            amount = self.amountOverride
        else:
            amount = self.amount

        self.canSave = True

        if amount.isEmpty: # unspecified amount
            return

        if self.invoiceType == QEInvoice.Type.LightningInvoice:
            if self.status in [PR_UNPAID, PR_FAILED]:
                if self.get_max_spendable_lightning() >= amount.satsInt:
                    lnaddr = self._effectiveInvoice._lnaddr
                    if lnaddr.amount and amount.satsInt < lnaddr.amount * COIN:
                        self.userinfo = _('Cannot pay less than the amount specified in the invoice')
                    else:
                        self.canPay = True
                elif self.address and self.get_max_spendable_onchain() > amount.satsInt:
                    # TODO: validate address?
                    # TODO: subtract fee?
                    self.canPay = True
                else:
                    self.userinfo = _('Insufficient balance')
            else:
                self.userinfo = {
                        PR_EXPIRED: _('Invoice is expired'),
                        PR_PAID: _('Invoice is already paid'),
                        PR_INFLIGHT: _('Invoice is already being paid'),
                        PR_ROUTING: _('Invoice is already being paid'),
                        PR_UNKNOWN: _('Invoice has unknown status'),
                    }[self.status]
        elif self.invoiceType == QEInvoice.Type.OnchainInvoice:
            if self.status in [PR_UNPAID, PR_FAILED]:
                if amount.isMax and self.get_max_spendable_onchain() > 0:
                    # TODO: dust limit?
                    self.canPay = True
                elif self.get_max_spendable_onchain() >= amount.satsInt:
                    # TODO: subtract fee?
                    self.canPay = True
                else:
                    self.userinfo = _('Insufficient balance')
            else:
                self.userinfo = {
                        PR_EXPIRED: _('Invoice is expired'),
                        PR_PAID: _('Invoice is already paid'),
                        PR_UNCONFIRMED: _('Invoice is already paid'),
                        PR_UNKNOWN: _('Invoice has unknown status'),
                    }[self.status]

    def setValidOnchainInvoice(self, invoice: Invoice):
        self._logger.debug('setValidOnchainInvoice')
        if invoice.is_lightning():
            raise Exception('unexpected LN invoice')
        self.set_effective_invoice(invoice)

    def setValidLightningInvoice(self, invoice: Invoice):
        self._logger.debug('setValidLightningInvoice')
        if not invoice.is_lightning():
            raise Exception('unexpected Onchain invoice')
        self.set_effective_invoice(invoice)

    def setValidLNURLPayRequest(self):
        self._logger.debug('setValidLNURLPayRequest')
        self.setInvoiceType(QEInvoice.Type.LNURLPayRequest)
        self._effectiveInvoice = None
        self.invoiceChanged.emit()

    def create_onchain_invoice(self, outputs, message, payment_request, uri):
        return self._wallet.wallet.create_invoice(
            outputs=outputs,
            message=message,
            pr=payment_request,
            URI=uri
            )

    def _on_round_1(self, pi: 'PaymentIdentifier'):
        if pi.needs_round_2():
            coro = pi.round_2(self._round_2_signal.emit, amount_sat=1)
            asyncio.run_coroutine_threadsafe(coro,  self._wallet.wallet.network.asyncio_loop)
        else:
            self.read_invoice(pi)

    def _on_round_2(self, pi: 'PaymentIdentifier'):
        self.read_invoice(pi)

    def _on_round_3(self, pi: 'PaymentIdentifier'):
        pass

    def validateRecipient(self, recipient):
        pi = PaymentIdentifier(self._wallet.wallet.config, None, recipient)
        if not pi.is_valid():
            self.setInvoiceType(QEInvoice.Type.Invalid)
            return

        recipient, amount, description, amount_required, validated = pi.get_fields_for_GUI(self._wallet.wallet)
        if pi.needs_round_1():
            coro = pi.round_1(on_success=self._round_1_signal.emit)
            asyncio.run_coroutine_threadsafe(coro, self._wallet.wallet.network.asyncio_loop)
            return
        self._read_invoice(pi)

    def read_invoice(self, pi):
        invoice = pi.get_invoice(self._wallet.wallet, amount_sat=0)
        if invoice.is_lightning():
            self.setValidLightningInvoice(invoice)
        else:
            self.setValidOnchainInvoice(invoice)
        self.validationSuccess.emit()

    def resolve_lnurl(self, lnurl):
        self._logger.debug('resolve_lnurl')
        url = decode_lnurl(lnurl)
        self._logger.debug(f'{repr(url)}')

        def resolve_task():
            try:
                coro = request_lnurl(url)
                fut = asyncio.run_coroutine_threadsafe(coro, self._wallet.wallet.network.asyncio_loop)
                self.on_lnurl(fut.result())
            except Exception as e:
                self.validationError.emit('lnurl', repr(e))

        threading.Thread(target=resolve_task).start()

    @pyqtSlot('quint64')
    @pyqtSlot('quint64', str)
    def lnurlGetInvoice(self, amount, comment=None):
        assert self._lnurlData

        amount = self.amountOverride.satsInt
        if self.lnurlData['min_sendable_sat'] != 0:
            try:
                assert amount >= self.lnurlData['min_sendable_sat']
                assert amount <= self.lnurlData['max_sendable_sat']
            except:
                self.lnurlError.emit('amount', _('Amount out of bounds'))
                return

        if self._lnurlData['comment_allowed'] == 0:
            comment = None

        self._logger.debug(f'fetching callback url {self._lnurlData["callback_url"]}')
        def fetch_invoice_task():
            try:
                params = { 'amount': amount * 1000 }
                if comment:
                    params['comment'] = comment
                coro = callback_lnurl(self._lnurlData['callback_url'], params)
                fut = asyncio.run_coroutine_threadsafe(coro, self._wallet.wallet.network.asyncio_loop)
                self.on_lnurl_invoice(fut.result())
            except Exception as e:
                self.lnurlError.emit('lnurl', repr(e))

        threading.Thread(target=fetch_invoice_task).start()

    def on_lnurl_invoice(self, invoice):
        self._logger.debug('on_lnurl_invoice')
        self._logger.debug(f'{repr(invoice)}')

        invoice = invoice['pr']
        self.recipient = invoice

    @pyqtSlot()
    def save_invoice(self):
        self.canSave = False
        if not self._effectiveInvoice:
            return

        self.key = self._effectiveInvoice.get_id()
        if self._wallet.wallet.get_invoice(self.key):
            self._logger.info(f'invoice {self.key} already exists')
        else:
            self._wallet.wallet.save_invoice(self._effectiveInvoice)
            self._wallet.invoiceModel.addInvoice(self.key)
            self.invoiceSaved.emit(self.key)


class QEUserEnteredPayment(QEInvoice):
    _logger = get_logger(__name__)

    validationError = pyqtSignal([str,str], arguments=['code','message'])
    invoiceCreateError = pyqtSignal([str,str], arguments=['code', 'message'])
    invoiceSaved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._amount = QEAmount()
        self.clear()

    recipientChanged = pyqtSignal()
    @pyqtProperty(str, notify=recipientChanged)
    def recipient(self):
        return self._recipient

    @recipient.setter
    def recipient(self, recipient: str):
        if self._recipient != recipient:
            self._recipient = recipient
            self.validate()
            self.recipientChanged.emit()

    messageChanged = pyqtSignal()
    @pyqtProperty(str, notify=messageChanged)
    def message(self):
        return self._message

    @message.setter
    def message(self, message):
        if self._message != message:
            self._message = message
            self.messageChanged.emit()

    amountChanged = pyqtSignal()
    @pyqtProperty(QEAmount, notify=amountChanged)
    def amount(self):
        return self._amount

    @amount.setter
    def amount(self, amount):
        if self._amount != amount:
            self._amount.copyFrom(amount)
            self.validate()
            self.amountChanged.emit()


    def validate(self):
        self.canPay = False
        self.canSave = False
        self._logger.debug('validate')

        if not self._recipient:
            self.validationError.emit('recipient', _('Recipient not specified.'))
            return

        if not bitcoin.is_address(self._recipient):
            self.validationError.emit('recipient', _('Invalid Bitcoin address'))
            return

        self.canSave = True

        if self._amount.isEmpty:
            self.validationError.emit('amount', _('Invalid amount'))
            return

        if self._amount.isMax:
            self.canPay = True
        else:
            if self.get_max_spendable_onchain() >= self._amount.satsInt:
                self.canPay = True

    @pyqtSlot()
    def save_invoice(self):
        assert self.canSave
        assert not self._amount.isMax

        self._logger.debug('saving invoice to %s, amount=%s, message=%s' % (self._recipient, repr(self._amount), self._message))

        inv_amt = self._amount.satsInt

        try:
            outputs = [PartialTxOutput.from_address_and_value(self._recipient, inv_amt)]
            self._logger.debug(repr(outputs))
            invoice = self._wallet.wallet.create_invoice(outputs=outputs, message=self._message, pr=None, URI=None)
        except InvoiceError as e:
            self.invoiceCreateError.emit('fatal', _('Error creating payment') + ':\n' + str(e))
            return

        self.key = invoice.get_id()
        self._wallet.wallet.save_invoice(invoice)
        self.invoiceSaved.emit()

    @pyqtSlot()
    def clear(self):
        self._recipient = None
        self._amount.clear()
        self._message = None
        self.canSave = False
        self.canPay = False
