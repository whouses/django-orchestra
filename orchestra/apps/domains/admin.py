import re

from django import forms
from django.contrib import admin
from django.utils.translation import ugettext_lazy as _

from orchestra.admin import ExtendedModelAdmin
from orchestra.admin.utils import admin_link, change_url
from orchestra.apps.accounts.admin import AccountAdminMixin
from orchestra.utils import apps

from .actions import view_zone
from .forms import RecordInlineFormSet, CreateDomainAdminForm
from .filters import TopDomainListFilter
from .models import Domain, Record


class RecordInline(admin.TabularInline):
    model = Record
    formset = RecordInlineFormSet
    verbose_name_plural = _("Extra records")
    
#    class Media:
#        css = {
#            'all': ('orchestra/css/hide-inline-id.css',)
#        }
#    
    def formfield_for_dbfield(self, db_field, **kwargs):
        """ Make value input widget bigger """
        if db_field.name == 'value':
            kwargs['widget'] = forms.TextInput(attrs={'size':'100'})
        if db_field.name == 'ttl':
            kwargs['widget'] = forms.TextInput(attrs={'size':'10'})
        return super(RecordInline, self).formfield_for_dbfield(db_field, **kwargs)


class DomainInline(admin.TabularInline):
    model = Domain
    fields = ('domain_link', 'display_records', 'account_link')
    readonly_fields = ('domain_link', 'display_records', 'account_link')
    extra = 0
    verbose_name_plural = _("Subdomains")
    
    domain_link = admin_link('__unicode__')
    domain_link.short_description = _("Name")
    account_link = admin_link('account')
    
    def display_records(self, domain):
        return ', '.join([record.type for record in domain.records.all()])
    display_records.short_description = _("Declared records")
    
    def has_add_permission(self, *args, **kwargs):
        return False
    
    def get_queryset(self, request):
        """ Order by structured name and imporve performance """
        qs = super(DomainInline, self).get_queryset(request)
        return qs.select_related('account').prefetch_related('records')


class DomainAdmin(AccountAdminMixin, ExtendedModelAdmin):
    list_display = (
        'structured_name', 'display_is_top', 'websites', 'account_link'
    )
    add_fields = ('name', 'account')
    fields = ('name', 'account_link')
    inlines = [RecordInline, DomainInline]
    list_filter = [TopDomainListFilter]
    change_readonly_fields = ('name',)
    search_fields = ['name',]
    add_form = CreateDomainAdminForm
    change_view_actions = [view_zone]
    
    def structured_name(self, domain):
        if not domain.is_top:
            return '&nbsp;'*4 + domain.name
        return domain.name
    structured_name.short_description = _("name")
    structured_name.allow_tags = True
    structured_name.admin_order_field = 'structured_name'
    
    def display_is_top(self, domain):
        return domain.is_top
    display_is_top.boolean = True
    display_is_top.admin_order_field = 'top'
    
    def websites(self, domain):
        if apps.isinstalled('orchestra.apps.websites'):
            webs = domain.websites.all()
            if webs:
                links = []
                for web in webs:
                    url = change_url(web)
                    links.append('<a href="%s">%s</a>' % (url, web.name))
                return '<br>'.join(links)
        return _("No website")
    websites.admin_order_field = 'websites__name'
    websites.short_description = _("Websites")
    websites.allow_tags = True
    
    def get_queryset(self, request):
        """ Order by structured name and imporve performance """
        qs = super(DomainAdmin, self).get_queryset(request)
        qs = qs.select_related('top', 'account')
        # For some reason if we do this we know for sure that join table will be called T4
        query = str(qs.query)
        table = re.findall(r'(T\d+)\."account_id"', query)[0]
        qs = qs.extra(
            select={
                'structured_name': 'CONCAT({table}.name, domains_domain.name)'.format(table=table)
            },
        ).order_by('structured_name')
        if apps.isinstalled('orchestra.apps.websites'):
            qs = qs.prefetch_related('websites')
        return qs
    
#    def save_related(self, request, form, formsets, change):
#        super(DomainAdmin, self).save_related(request, form, formsets, change)
#        if form.cleaned_data['migrate_subdomains']:
#            domain = form.instance
#            domain.subdomains.update(account_id=domain.account_id)


admin.site.register(Domain, DomainAdmin)
