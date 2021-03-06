import datetime
from dateutil.relativedelta import relativedelta
from functools import lru_cache

from django.core.validators import ValidationError, RegexValidator
from django.db import models
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.template import loader, Context
from django.utils import timezone, translation
from django.utils.encoding import force_text
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _

from orchestra.contrib.accounts.models import Account
from orchestra.contrib.contacts.models import Contact
from orchestra.core import validators
from orchestra.utils.html import html_to_pdf

from . import settings


class BillContact(models.Model):
    account = models.OneToOneField('accounts.Account', verbose_name=_("account"),
        related_name='billcontact')
    name = models.CharField(_("name"), max_length=256, blank=True,
        help_text=_("Account full name will be used when left blank."))
    address = models.TextField(_("address"))
    city = models.CharField(_("city"), max_length=128,
        default=settings.BILLS_CONTACT_DEFAULT_CITY)
    zipcode = models.CharField(_("zip code"), max_length=10,
        validators=[RegexValidator(r'^[0-9A-Z]{3,10}$', _("Enter a valid zipcode."))])
    country = models.CharField(_("country"), max_length=20,
        choices=settings.BILLS_CONTACT_COUNTRIES,
        default=settings.BILLS_CONTACT_DEFAULT_COUNTRY)
    vat = models.CharField(_("VAT number"), max_length=64)
    
    def __str__(self):
        return self.name
    
    def get_name(self):
        return self.name or self.account.get_full_name()
    
    def clean(self):
        self.vat = self.vat.strip()
        self.city = self.city.strip()
        validators.all_valid({
            'vat': (validators.validate_vat, self.vat, self.country),
            'zipcode': (validators.validate_zipcode, self.zipcode, self.country)
        })


class BillManager(models.Manager):
    def get_queryset(self):
        queryset = super(BillManager, self).get_queryset()
        if self.model != Bill:
            bill_type = self.model.get_class_type()
            queryset = queryset.filter(type=bill_type)
        return queryset


class Bill(models.Model):
    OPEN = ''
    CREATED = 'CREATED'
    PROCESSED = 'PROCESSED'
    AMENDED = 'AMENDED'
    PAID = 'PAID'
    EXECUTED = 'EXECUTED'
    BAD_DEBT = 'BAD_DEBT'
    INCOMPLETE = 'INCOMPLETE'
    PAYMENT_STATES = (
        (OPEN, _("Open")),
        (CREATED, _("Created")),
        (PROCESSED, _("Processed")),
        (AMENDED, _("Amended")),
        (PAID, _("Paid")),
        (INCOMPLETE, _('Incomplete')),
        (EXECUTED, _("Executed")),
        (BAD_DEBT, _("Bad debt")),
    )
    BILL = 'BILL'
    INVOICE = 'INVOICE'
    AMENDMENTINVOICE = 'AMENDMENTINVOICE'
    FEE = 'FEE'
    AMENDMENTFEE = 'AMENDMENTFEE'
    PROFORMA = 'PROFORMA'
    TYPES = (
        (INVOICE, _("Invoice")),
        (AMENDMENTINVOICE, _("Amendment invoice")),
        (FEE, _("Fee")),
        (AMENDMENTFEE, _("Amendment Fee")),
        (PROFORMA, _("Pro forma")),
    )
    AMEND_MAP = {
        INVOICE: AMENDMENTINVOICE,
        FEE: AMENDMENTFEE,
    }
    
    number = models.CharField(_("number"), max_length=16, unique=True, blank=True)
    account = models.ForeignKey('accounts.Account', verbose_name=_("account"),
        related_name='%(class)s')
    amend_of = models.ForeignKey('self', null=True, blank=True, verbose_name=_("amend of"),
        related_name='amends')
    type = models.CharField(_("type"), max_length=16, choices=TYPES)
    created_on = models.DateField(_("created on"), auto_now_add=True)
    closed_on = models.DateField(_("closed on"), blank=True, null=True, db_index=True)
    is_open = models.BooleanField(_("open"), default=True)
    is_sent = models.BooleanField(_("sent"), default=False)
    due_on = models.DateField(_("due on"), null=True, blank=True)
    updated_on = models.DateField(_("updated on"), auto_now=True)
#    total = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    comments = models.TextField(_("comments"), blank=True)
    html = models.TextField(_("HTML"), blank=True)
    
    objects = BillManager()
    
    class Meta:
        get_latest_by = 'id'
    
    def __str__(self):
        return self.number
    
    @classmethod
    def get_class_type(cls):
        return cls.__name__.upper()
    
    @cached_property
    def total(self):
        return self.compute_total()
    
    @cached_property
    def seller(self):
        return Account.objects.get_main().billcontact
    
    @cached_property
    def buyer(self):
        return self.account.billcontact
    
    @property
    def has_multiple_pages(self):
        return self.type != self.FEE
    
    @cached_property
    def payment_state(self):
        if self.is_open or self.get_type() == self.PROFORMA:
            return self.OPEN
        secured = 0
        pending = 0
        created = False
        processed = False
        executed = False
        rejected = False
        for transaction in self.transactions.all():
            if transaction.state == transaction.SECURED:
                secured += transaction.amount
                pending += transaction.amount
            elif transaction.state == transaction.WAITTING_PROCESSING:
                pending += transaction.amount
                created = True
            elif transaction.state == transaction.WAITTING_EXECUTION:
                pending += transaction.amount
                processed = True
            elif transaction.state == transaction.EXECUTED:
                pending += transaction.amount
                executed = True
            elif transaction.state == transaction.REJECTED:
                rejected = True
            else:
                raise TypeError("Unknown state")
        ongoing = bool(secured != 0 or created or processed or executed)
        total = self.compute_total()
        if total >= 0:
            if secured >= total:
                return self.PAID
            elif ongoing and pending < total:
                return self.INCOMPLETE
        else:
            if secured <= total:
                return self.PAID
            elif ongoing and pending > total:
                return self.INCOMPLETE
        if created:
            return self.CREATED
        elif processed:
            return self.PROCESSED
        elif executed:
            return self.EXECUTED
        return self.BAD_DEBT
    
    def clean(self):
        if self.amend_of_id:
            errors = {}
            if self.type not in self.AMEND_MAP.values():
                errors['amend_of'] = _("Type %s is not an amendment.") % self.get_type_display()
            if self.amend_of.account_id != self.account_id:
                errors['account'] = _("Amend of related account doesn't match bill account.")
            if self.amend_of.is_open:
                errors['amend_of'] = _("Related invoice is in open state.")
            if self.amend_of.type in self.AMEND_MAP.values():
                errors['amend_of'] = _("Related invoice is an amendment.")
            if errors:
                raise ValidationError(errors)
    
    def get_payment_state_display(self):
        value = self.payment_state
        return force_text(dict(self.PAYMENT_STATES).get(value, value))
    
    def get_current_transaction(self):
        return self.transactions.exclude_rejected().first()
    
    def get_type(self):
        return self.type or self.get_class_type()
    
    def get_amend_type(self):
        amend_type = self.AMEND_MAP.get(self.type)
        if amend_type is None:
            raise TypeError("%s has no associated amend type." % self.type)
        return amend_type
    
    def get_number(self):
        cls = type(self)
        bill_type = self.get_type()
        if bill_type == self.BILL:
            raise TypeError('This method can not be used on BILL instances')
        bill_type = bill_type.replace('AMENDMENT', 'AMENDMENT_')
        prefix = getattr(settings, 'BILLS_%s_NUMBER_PREFIX' % bill_type)
        if self.is_open:
            prefix = 'O{}'.format(prefix)
        bills = cls.objects.filter(number__regex=r'^%s[1-9]+' % prefix)
        last_number = bills.order_by('-number').values_list('number', flat=True).first()
        if last_number is None:
            last_number = 0
        else:
            last_number = int(last_number[len(prefix)+4:])
        number = last_number + 1
        year = timezone.now().strftime("%Y")
        number_length = settings.BILLS_NUMBER_LENGTH
        zeros = (number_length - len(str(number))) * '0'
        number = zeros + str(number)
        return '{prefix}{year}{number}'.format(prefix=prefix, year=year, number=number)
    
    def get_due_date(self, payment=None):
        now = timezone.now()
        if payment:
            return now + payment.get_due_delta()
        return now + relativedelta(months=1)
    
    def close(self, payment=False):
        if not self.is_open:
            raise TypeError("Bill not in Open state.")
        if payment is False:
            payment = self.account.paymentsources.get_default()
        if not self.due_on:
            self.due_on = self.get_due_date(payment=payment)
        total = self.compute_total()
        transaction = None
        if self.get_type() != self.PROFORMA:
            transaction = self.transactions.create(bill=self, source=payment, amount=total)
        self.closed_on = timezone.now()
        self.is_open = False
        self.is_sent = False
        self.number = self.get_number()
        self.html = self.render(payment=payment)
        self.save()
        return transaction
    
    def send(self):
        pdf = self.as_pdf()
        self.account.send_email(
            template=settings.BILLS_EMAIL_NOTIFICATION_TEMPLATE,
            context={
                'bill': self,
                'settings': settings,
            },
            email_from=settings.BILLS_SELLER_EMAIL,
            contacts=(Contact.BILLING,),
            attachments=[
                ('%s.pdf' % self.number, pdf, 'application/pdf')
            ]
        )
        self.is_sent = True
        self.save(update_fields=['is_sent'])
    
    def render(self, payment=False, language=None):
        with translation.override(language or self.account.language):
            if payment is False:
                payment = self.account.paymentsources.get_default()
            context = Context({
                'bill': self,
                'lines': self.lines.all().prefetch_related('sublines'),
                'seller': self.seller,
                'buyer': self.buyer,
                'seller_info': {
                    'phone': settings.BILLS_SELLER_PHONE,
                    'website': settings.BILLS_SELLER_WEBSITE,
                    'email': settings.BILLS_SELLER_EMAIL,
                    'bank_account': settings.BILLS_SELLER_BANK_ACCOUNT,
                },
                'currency': settings.BILLS_CURRENCY,
                'payment': payment and payment.get_bill_context(),
                'default_due_date': self.get_due_date(payment=payment),
                'now': timezone.now(),
            })
            template_name = 'BILLS_%s_TEMPLATE' % self.get_type()
            template = getattr(settings, template_name, settings.BILLS_DEFAULT_TEMPLATE)
            bill_template = loader.get_template(template)
            html = bill_template.render(context)
            html = html.replace('-pageskip-', '<pdf:nextpage />')
        return html
    
    def as_pdf(self):
        html = self.html or self.render()
        return html_to_pdf(html, pagination=self.has_multiple_pages)
    
    def save(self, *args, **kwargs):
        if not self.type:
            self.type = self.get_type()
        if not self.number:
            self.number = self.get_number()
        super(Bill, self).save(*args, **kwargs)
    
    @lru_cache()
    def compute_subtotals(self):
        subtotals = {}
        lines = self.lines.annotate(totals=F('subtotal') + Sum(Coalesce('sublines__total', 0)))
        for tax, total in lines.values_list('tax', 'totals'):
            try:
                subtotals[tax] += total
            except KeyError:
                subtotals[tax] = total
        result = {}
        for tax, subtotal in subtotals.items():
            result[tax] = (subtotal, round(tax/100*subtotal, 2))
        return result
    
    @lru_cache()
    def compute_base(self):
        bases = self.lines.annotate(
            bases=F('subtotal') + Sum(Coalesce('sublines__total', 0))
        )
        return round(bases.aggregate(Sum('bases'))['bases__sum'] or 0, 2)
    
    @lru_cache()
    def compute_tax(self):
        taxes = self.lines.annotate(
            taxes=(F('subtotal') + Coalesce(Sum('sublines__total'), 0)) * (F('tax')/100)
        )
        return round(taxes.aggregate(Sum('taxes'))['taxes__sum'] or 0, 2)
    
    @lru_cache()
    def compute_total(self):
        if 'lines' in getattr(self, '_prefetched_objects_cache', ()):
            total = 0
            for line in self.lines.all():
                line_total = line.compute_total()
                total += line_total * (1+line.tax/100)
            return round(total, 2)
        else:
            totals = self.lines.annotate(
                totals=(F('subtotal') + Sum(Coalesce('sublines__total', 0))) * (1+F('tax')/100)
            )
            return round(totals.aggregate(Sum('totals'))['totals__sum'] or 0, 2)


class Invoice(Bill):
    class Meta:
        proxy = True


class AmendmentInvoice(Bill):
    class Meta:
        proxy = True


class Fee(Bill):
    class Meta:
        proxy = True


class AmendmentFee(Bill):
    class Meta:
        proxy = True


class ProForma(Bill):
    class Meta:
        proxy = True


class BillLine(models.Model):
    """ Base model for bill item representation """
    bill = models.ForeignKey(Bill, verbose_name=_("bill"), related_name='lines')
    description = models.CharField(_("description"), max_length=256)
    rate = models.DecimalField(_("rate"), blank=True, null=True, max_digits=12, decimal_places=2)
    quantity = models.DecimalField(_("quantity"), blank=True, null=True, max_digits=12,
        decimal_places=2)
    verbose_quantity = models.CharField(_("Verbose quantity"), max_length=16)
    subtotal = models.DecimalField(_("subtotal"), max_digits=12, decimal_places=2)
    tax = models.DecimalField(_("tax"), max_digits=4, decimal_places=2)
    start_on = models.DateField(_("start"))
    end_on = models.DateField(_("end"), null=True, blank=True)
    order = models.ForeignKey(settings.BILLS_ORDER_MODEL, null=True, blank=True,
        help_text=_("Informative link back to the order"), on_delete=models.SET_NULL)
    order_billed_on = models.DateField(_("order billed"), null=True, blank=True)
    order_billed_until = models.DateField(_("order billed until"), null=True, blank=True)
    created_on = models.DateField(_("created"), auto_now_add=True)
    # Amendment
    amended_line = models.ForeignKey('self', verbose_name=_("amended line"),
        related_name='amendment_lines', null=True, blank=True)
    
    def __str__(self):
        return "#%i" % self.pk
    
    def get_verbose_quantity(self):
        return self.verbose_quantity or self.quantity
    
    def get_verbose_period(self):
        from django.template.defaultfilters import date
        date_format = "N 'y"
        if self.start_on.day != 1 or self.end_on.day != 1:
            date_format = "N j, 'y"
            end = date(self.end_on, date_format)
        else:
            end = date((self.end_on - datetime.timedelta(days=1)), date_format)
        ini = date(self.start_on, date_format).capitalize()
        end = end.capitalize()
        if not self.end_on:
            return ini
        if ini == end:
            return ini
        return "{ini} / {end}".format(ini=ini, end=end)
    
    @lru_cache()
    def compute_total(self):
        total = self.subtotal or 0
        if hasattr(self, 'subline_total'):
            total += self.subline_total or 0
        elif 'sublines' in getattr(self, '_prefetched_objects_cache', ()):
            total += sum(subline.total for subline in self.sublines.all())
        else:
            total += self.sublines.aggregate(sub_total=Sum('total'))['sub_total'] or 0
        return round(total, 2)


class BillSubline(models.Model):
    """ Subline used for describing an item discount """
    VOLUME = 'VOLUME'
    COMPENSATION = 'COMPENSATION'
    OTHER = 'OTHER'
    TYPES = (
        (VOLUME, _("Volume")),
        (COMPENSATION, _("Compensation")),
        (OTHER, _("Other")),
    )
    
    # TODO: order info for undoing
    line = models.ForeignKey(BillLine, verbose_name=_("bill line"), related_name='sublines')
    description = models.CharField(_("description"), max_length=256)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    type = models.CharField(_("type"), max_length=16, choices=TYPES, default=OTHER)
    
    def __str__(self):
        return "%s %i" % (self.description, self.total)
