import random
import string
from lxml import etree
from lxml.builder import E

from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_iban.validators import IBANValidator, IBAN_COUNTRY_CODE_LENGTH
from rest_framework import serializers

from orchestra.utils import plugins

from .forms import BankTransferForm, CreditCardForm


class PaymentMethod(plugins.Plugin):
    label_field = 'label'
    number_field = 'number'
    
    __metaclass__ = plugins.PluginMount
    
    def get_form(self):
        self.form.plugin = self
        return self.form
    
    def get_serializer(self):
        self.serializer.plugin = self
        return self.serializer
    
    def get_label(self, data):
        return data[self.label_field]
    
    def get_number(self, data):
        return data[self.number_field]


class BankTransferSerializer(serializers.Serializer):
    iban = serializers.CharField(label='IBAN', validators=[IBANValidator()],
            min_length=min(IBAN_COUNTRY_CODE_LENGTH.values()), max_length=34)
    name = serializers.CharField(label=_("Name"), max_length=128)


class CreditCardSerializer(serializers.Serializer):
    pass


class BankTransfer(PaymentMethod):
    verbose_name = _("Bank transfer")
    label_field = 'name'
    number_field = 'iban'
    form = BankTransferForm
    serializer = BankTransferSerializer
    
    def set_id(self):
        size=6
        chars=string.ascii_uppercase + string.digits
        self.payment_id =  ''.join(random.choice(chars) for _ in range(size))
    
    def _process_transactions(self, transactions):
        for transaction in transactions:
            account = transaction.account
            data = transaction.data
            transaction.info = self.payment_id
            transaction.state = transaction.WAITTING_CONFIRMATION
            transaction.save()
            yield E.DrctDbtTxInf(                           # Direct Debit Transaction Info
                E.PmtId(                                    # Payment Id
                    E.EndToEndId(str(transaction.id))       # Payment Id/End to End
                ),
                E.InstdAmt(transaction.amount, Ccy="EUR"),  # Instructed Amount
                E.DrctDbtTx(                                # Direct Debit Transaction
                    E.MndtRltdInf(                          # Mandate Related Info
                        E.MndtId(str(account.id)),          # Mandate Id
                        E.DtOfSgntr(                        # Date of Signature
                            account.registered_on.strfrm("%Y-%m-%d")
                        )
                    )
                ),
                E.DbtrAgt(                                  # Debtor Agent
                    E.FinInstnId(                           # Financial Institution Id
                        E.Othr(
                            E.Id('NOTPROVIDED')
                        )
                    )
                ),
                E.Dbtr(                                     # Debtor
                    E.Nm(account.name),                     # Name
                ),
                E.DbtrAcct(                                 # Debtor Account
                    E.Id(
                        E.IBAN(data['iban'])
                    ),
                ),
            )
    
    def process(self, transactions)
        self.set_id()
        creditor_name = settings.PAYMENTS_DD_CREDITOR_NAME
        creditor_iban = settings.PAYMENTS_DD_CREDITOR_IBAN
        creditor_bic = settings.PAYMENTS_DD_CREDITOR_BIC
        creditor_at02_id = settings.PAYMENTS_DD_CREDITOR_AT02_ID
        now = timezone.now()
        total = str(sum([transaction.amount for transaction in transactions]))
        sepa = E.Document(
            E.CstmrDrctDbtInitn(
                E.GrpHdr(                                   # Group Header
                    E.MsgId(self.payment_id),               # Message Id
                    E.CreDtTm(now.strftime("%Y-%m-%dT%H:%M:%S")), # Creation Date Time
                    E.NbOfTxs(str(len(transactions))),      # Number of Transactions
                    E.CtrlSum(total),                       # Control Sum
                    E.InitgPty(                             # Initiating Party
                        E.Nm(creditor_name),                # Name
                        E.Id(                               # Identification
                            E.OrgId(                        # Organisation Id
                                E.Othr(
                                    E.Id(creditor_at_02)
                                )
                            )
                        )
                    )
                ),
                E.PmtInf(                                   # Payment Info
                    E.PmtInfId(self.payment_id),            # Payment Id
                    E.PmtMtd("DD"),                         # Payment Method
                    E.NbOfTxs(str(len(transactions))),      # Number of Transactions
                    E.CtrlSum(total),                       # Control Sum
                    E.PmtTpInf(                             # Payment Type Info
                        E.SvcLvl(                           # Service Level
                            E.Cd("SEPA")                    # Code
                        ),
                        E.LclInstrm(                        # Local Instrument
                            E.Cd("CORE")                    # Code
                        ),
                        E.SeqTp("RCUR")                     # Sequence Type
                    ),
                    E.ReqdColltnDt(now.strfrm("%Y-%m-%d")), # Requested Collection Date
                    E.Cdtr(                                 # Creditor
                        E.Nm(creditor_name)
                    ),
                    E.CdtrAcct(                             # Creditor Account
                        E.Id(
                            E.IBAN(creditor_iban)
                        )
                    ),
                    E.CdtrAgt(                              # Creditor Agent
                        E.FinInstnId(                       # Financial Institution Id
                            E.BIC(creditor_bic)
                        )
                    ),
                *list(self._process_transactions(transactions))   # Transactions
                )
            ), {
                'xmlns': "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02",
                'xmlns:xsi': "http://www.w3.org/2001/XMLSchema-instance"
            }
        )
        # http://www.iso20022.org/documents/messages/1_0_version/pain/schemas/pain.008.001.02.zip
        schema = etree.parse('pain.008.001.02.xsd')
        schema.assertValid(sepa)
        # TODO where to save this shit?
        # TODO new model? Payment with batch support, How this relates to transaction?
        return etree.tostring(page, pretty_print=True, xml_declaration=True)


class CreditCard(PaymentMethod):
    verbose_name = _("Credit card")
    form = CreditCardForm
    serializer = CreditCardSerializer
