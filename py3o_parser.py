# -*- encoding: utf-8 -*-
from cStringIO import StringIO
import json
import pkg_resources
import os
import sys
from base64 import b64decode
import requests
from tempfile import NamedTemporaryFile
from openerp import _
from openerp import exceptions
from openerp.report.report_sxw import report_sxw, rml_parse
from openerp import registry

from py3o.template.helpers import Py3oConvertor
from py3o.template import Template

libreoffice_present = os.path.expanduser('~/.py3o_libreoffice_present')
if os.path.exists(libreoffice_present):
    USE_LOCAL_LIBREOFFICE = open(libreoffice_present).read() == 'True'

else:
    USE_LOCAL_LIBREOFFICE = False
    try:
        import sh
        import tempfile

        sh.libreoffice('--headless', '--version')

        USE_LOCAL_LIBREOFFICE = True

    except:
        pass

    open(libreoffice_present, 'w').write(str(USE_LOCAL_LIBREOFFICE))


_extender_functions = {}


class TemplateNotFound(Exception):
    pass


def py3o_report_extender(report_name):
    """
    A decorator to define function to extend the context sent to a template.
    This will be called at the creation of the report.
    The following arguments will be passed to it:
        - pool: the model pool
        - cr: the database cursor
        - uid: the id of the user that call the renderer
        - localcontext: The context that will be passed to the report engine
        - context: the Odoo context

    Method copied from CampToCamp report_webkit module.

    :param report_name: xml id of the report
    :return: a decorated class
    """
    def fct1(fct):
        lst = _extender_functions.get(report_name)
        if not lst:
            lst = []
            _extender_functions[report_name] = lst
        lst.append(fct)
        return fct
    return fct1


class Py3oParser(report_sxw):
    """Custom class that use Py3o to render libroffice reports.
        Code partially taken from CampToCamp's webkit_report."""

    def __init__(self, name, table, rml=False, parser=rml_parse,
                 header=False, store=False, register=True):
        self.localcontext = {}
        super(Py3oParser, self).__init__(
            name, table, rml=rml, parser=parser,
            header=header, store=store, register=register
        )

    def get_template(self, report_obj):
        """private helper to fetch the template data either from the database
        or from the default template file provided by the implementer.

        ATM this method takes a report definition recordset
        to try and fetch the report template from database. If not found it will
        fallback to the template file referenced in the report definition.

        @param report_obj: a recordset representing the report defintion
        @type report_obj: openerp.model.recordset instance

        @returns: string or buffer containing the template data

        @raises: TemplateNotFound which is a subclass of
        openerp.exceptions.DeferredException
        """

        tmpl_data = None

        if report_obj.py3o_template_id and report_obj.py3o_template_id.id:
            # if a user gave a report template
            tmpl_data = b64decode(
                report_obj.py3o_template_id.py3o_template_data
            )

        elif report_obj.py3o_template_fallback and report_obj.module:
            # if the default is defined
            flbk_filename = pkg_resources.resource_filename(
                "openerp.addons.%s" % report_obj.module,
                report_obj.py3o_template_fallback,
            )
            if os.path.exists(flbk_filename):
                # and it exists on the fileystem
                with open(flbk_filename, 'r') as tmpl:
                    tmpl_data = tmpl.read()

        if tmpl_data is None:
            # if for any reason the template is not found
            raise TemplateNotFound(
                _(u'No template found. Aborting.'),
                sys.exc_info(),
            )

        return tmpl_data

    def create_single_pdf(self, cr, uid, ids, data, report_xml, context=None):
        """ Overide this function to generate our py3o report
        """
        if report_xml.report_type != 'py3o':
            return super(Py3oParser, self).create_single_pdf(
                cr, uid, ids, data, report_xml, context=context
            )

        pool = registry(cr.dbname)
        model_data_ids = pool['ir.model.data'].search(
            cr, uid, [
                ('model', '=', 'ir.actions.report.xml'),
                ('res_id', '=', report_xml.id),
            ]
        )

        xml_id = None
        if model_data_ids:
            model_data = pool['ir.model.data'].browse(
                cr, uid, model_data_ids[0], context=context
            )
            xml_id = '%s.%s' % (model_data.module, model_data.name)

        parser_instance = self.parser(cr, uid, self.name2, context=context)
        parser_instance.set_context(
            self.getObjects(cr, uid, ids, context),
            data, ids, report_xml.report_type
        )

        if xml_id in _extender_functions:
            for fct in _extender_functions[xml_id]:
                fct(pool, cr, uid, parser_instance.localcontext, context)

        tmpl_data = self.get_template(report_xml)

        in_stream = StringIO(tmpl_data)
        out_stream = StringIO()
        template = Template(in_stream, out_stream)
        expressions = template.get_all_user_python_expression()
        py_expression = template.convert_py3o_to_python_ast(expressions)
        convertor = Py3oConvertor()
        data_struct = convertor(py_expression)

        filetype = report_xml.py3o_fusion_filetype

        datadict = parser_instance.localcontext

        parsed_datadict = data_struct.render(datadict)

        fusion_server_obj = pool.get('py3o.server')
        fusion_server_ids = fusion_server_obj.search(
            cr, uid, [('is_active', '=', True)], context=context, limit=1
        )
        if not fusion_server_ids:
            if filetype.fusion_ext == report_xml.py3o_template_id.filetype:
                # No format conversion is needed, render the template directly
                template.render(parsed_datadict)
                res = out_stream.getvalue()

            else:
                if USE_LOCAL_LIBREOFFICE:
                    import sh
                    import tempfile

                    template.render(parsed_datadict)
                    res = out_stream.getvalue()

                    arq = tempfile.NamedTemporaryFile(delete=False)
                    arq.seek(0)
                    arq.write(res)
                    arq.flush()

                    res_arq_name = arq.name + '.' + filetype.fusion_ext

                    sh.libreoffice('--headless', '--invisible', \
                        '--convert-to', filetype.fusion_ext, \
                        '--outdir', '/tmp', arq.name)

                    res = file(res_arq_name, 'r').read()

                    os.remove(res_arq_name)
                    os.remove(arq.name)

                else:
                    raise exceptions.MissingError(
                        _(u"No Py3o server configuration found")
                    )

        else:  # Call py3o.server to render the template in the desired format
            fusion_server_id = fusion_server_ids[0]

            fusion_server = fusion_server_obj.browse(
                cr, uid, fusion_server_id, context=context
            )
            in_stream.seek(0)
            files = {
                'tmpl_file': in_stream,
            }
            fields = {
                "targetformat": filetype.fusion_ext,
                "datadict": json.dumps(parsed_datadict),
                "image_mapping": "{}",
            }
            r = requests.post(fusion_server.url, data=fields, files=files)
            if r.status_code != 200:
                # server says we have an issue... let's tell that to enduser
                raise exceptions.Warning(
                    _('Fusion server error %s') % r.text,
                )

            # Here is a little joke about Odoo
            # we do nice chunked reading from the network...
            chunk_size = 1024
            with NamedTemporaryFile(
                    suffix=filetype.human_ext,
                    prefix='py3o-template-'
            ) as fd:
                for chunk in r.iter_content(chunk_size):
                    fd.write(chunk)
                fd.seek(0)
                # ... but odoo wants the whole data in memory anyways :)
                res = fd.read()

        return res, filetype.human_ext

    def create(self, cr, uid, ids, data, context=None):
        """ Override this function to handle our py3o report
        """
        pool = registry(cr.dbname)
        ir_action_report_obj = pool['ir.actions.report.xml']
        report_xml_ids = ir_action_report_obj.search(
            cr, uid, [('report_name', '=', self.name[7:])], context=context
        )
        if not report_xml_ids:
            return super(Py3oParser, self).create(
                cr, uid, ids, data, context=context
            )

        report_xml = ir_action_report_obj.browse(
            cr, uid, report_xml_ids[0], context=context
        )

        result = self.create_source_pdf(
            cr, uid, ids, data, report_xml, context
        )

        if not result:
            return False, False
        return result
