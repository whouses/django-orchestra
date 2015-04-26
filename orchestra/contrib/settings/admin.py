from functools import partial

from django.contrib import admin, messages
from django.db import models

from django.views.generic.edit import FormView
from django.utils.translation import ngettext, ugettext_lazy as _

from orchestra.settings import Setting
from orchestra.utils import sys, paths

from . import parser
from .forms import SettingFormSet


class SettingView(FormView):
    template_name = 'admin/settings/change_form.html'
    form_class = SettingFormSet
    success_url = '.'
    
    def get_context_data(self, **kwargs):
        context = super(SettingView, self).get_context_data(**kwargs)
        context.update({
            'title': _("Change settings"),
            'settings_file': parser.get_settings_file(),
        })
        return context
    
    def get_initial(self):
        initial_data = []
        prev_app = None
        account = 0
        for name, setting in Setting.settings.items():
            app = name.split('_')[0]
            initial = {
                'name': setting.name,
                'help_text': setting.help_text,
                'default': setting.default,
                'type': type(setting.default),
                'value': setting.value,
                'choices': setting.choices,
                'app': app,
                'editable': setting.editable,
                'multiple': setting.multiple,
            }
            if app == 'ORCHESTRA':
                initial_data.insert(account, initial)
                account += 1
            else:
                initial_data.append(initial)
        return initial_data
    
    def form_valid(self, form):
        settings = Setting.settings
        changes = {}
        for data in form.cleaned_data:
            setting = settings[data['name']]
            if not isinstance(data['value'], parser.NotSupported) and setting.editable:
                if setting.value != data['value']:
                    if setting.default == data['value']:
                        changes[setting.name] = parser.Remove()
                    else:
                        changes[setting.name] = parser.serialize(data['value'])
        if changes:
            # Display confirmation
            if not self.request.POST.get('confirmation'):
                settings_file = parser.get_settings_file()
                new_content = parser.apply(changes)
                diff = sys.run("cat <<EOF | diff %s -\n%s\nEOF" % (settings_file, new_content), error_codes=[1, 0]).stdout
                context = self.get_context_data(form=form)
                context['diff'] = diff
                return self.render_to_response(context)
            
            # Save changes
            parser.save(changes)
            n = len(changes)
            messages.success(self.request, ngettext(
                _("One change successfully applied, the orchestra is going to be restarted..."),
                _("%s changes successfully applied, the orchestra is going to be restarted...") % n,
                n)
            )
            # TODO find aonther way without root and implement reload
#            sys.run('echo { sleep 2 && python3 %s/manage.py reload; } &' % paths.get_site_dir(), async=True)
        else:
            messages.success(self.request, _("No changes have been detected."))
        return super(SettingView, self).form_valid(form)


admin.site.register_url(r'^settings/setting/$', SettingView.as_view(), 'settings_edit_settings')
