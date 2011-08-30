import itertools as it
import re

from lxml import etree as et

from pyws.errors import BadRequest, BadAuthData
from pyws.functions.args import List, Dict, TypeFactory, Type, DICT_NAME_KEY
from pyws.response import Response
from pyws.protocols.base import Protocol

from utils import *
from wsdl import WsdlGenerator

TAG_NAME_RE = re.compile('{(.*?)}(.*)')
ENCODING = 'utf-8'

def get_element_name(el):
    name = el.tag
    mo = TAG_NAME_RE.search(name)
    if mo:
        return mo.group(1), mo.group(2)
    return None, name

NIL = '{http://www.w3.org/2001/XMLSchema-instance}nil'

def xml2obj(xml, schema):
    children = xml.getchildren()
    if not children:
        if xml.text is None:
            result = None
        else:
            result = unicode(xml.text)
    elif issubclass(schema, List):
        result = []
        for child in children:
            result.append(xml2obj(child, schema.element_type))
    elif issubclass(schema, Dict):
        result = {}
        schema = dict((field.name, field.type) for field in schema.fields)
        for child in children:
            name = get_element_name(child)[1]
            obj = xml2obj(child, schema[name])
            if name not in result:
                result[name] = obj
            else:
                if not isinstance(result[name], list):
                    result[name] = [result[name]]
                result[name].append(obj)
    else:
        raise BadRequest('Couldn\'t decode XML')
    return result

def obj2xml(root, contents, schema=None, namespace=None):
    kwargs = namespace and {'namespace': namespace} or {}
    if isinstance(contents, (list, tuple)):
        for item in contents:
            element = et.SubElement(root, 'item', **kwargs)
            obj2xml(
                element,
                item,
                schema and schema.element_type,
                namespace
            )
    elif isinstance(contents, dict):
        fields = schema and dict((f.name, f.type) for f in schema.fields) or {}
        for name, item in contents.iteritems():
            element = et.SubElement(root, name, **kwargs)
            obj2xml(
                element,
                item,
                fields.get(name),
                namespace
            )
    elif contents is not None:
        root.text = \
            schema and schema.serialize(contents) or Type.serialize(contents)
    elif contents is None:
        root.set(NIL, 'true')
    return root

def get_axis_package_name(ns):
    mo = re.search('https?://([\\w\\.-]+).*?/(.*)', ns)
    if not mo:
        raise Exception('No domain in service namespace')
    res = list(reversed(mo.group(1).split('.')))
    return '.'.join(it.ifilter(lambda s: s, it.imap(
        lambda s: re.sub('[^\w]', '_', s), res + mo.group(2).split('/'))))


class HeadersAuthDataGetter(object):

    def __init__(self, headers_schema):
        self.headers_schema = headers_schema

    def __call__(self, request):

        env = request.parsed_data.xml. \
            xpath('/se:Envelope', namespaces=SoapProtocol.namespaces)[0]

        header = env.xpath(
            './se:Header/*', namespaces=SoapProtocol.namespaces)
        if len(header) != 1:
            raise BadAuthData()
        header = header[0]

        return xml2obj(header, self.headers_schema)


class ParsedData(object):
    pass


class SoapProtocol(Protocol):

    name = 'soap'
    namespaces = {'se': SOAP_ENV_NS}

    def __init__(self, service_name, tns, location, *args, **kwargs):
        super(SoapProtocol, self).__init__(*args, **kwargs)
        self.service_name = service_name
        self.tns = tns
        self.location = location

    def parse_request(self, request):

        if hasattr(request, 'parsed_data'):
            return request.parsed_data

        request.parsed_data = ParsedData()

        xml = et.fromstring(request.text.encode(ENCODING))

        env = xml.xpath('/se:Envelope', namespaces=self.namespaces)

        if not len(env):
            raise BadRequest('No {%s}Envelope element.' % SOAP_ENV_NS)
        env = env[0]

        body = env.xpath('./se:Body', namespaces=self.namespaces)

        if not len(body):
            raise BadRequest('No {%s}Body element.' % SOAP_ENV_NS)
        if len(body) > 1:
            raise BadRequest(
                'There must be only one {%s}Body element.' % SOAP_ENV_NS)
        body = body[0]

        func = body.getchildren()
        if not len(func):
            raise BadRequest(
                '{%s}Body element has no child elements.' % SOAP_ENV_NS)
        if len(func) > 1:
            raise BadRequest('{%s}Body element '
                'has more than one child element.' % SOAP_ENV_NS)
        func = func[0]

        func_name = get_element_name(func)[1]
        if func_name.endswith('_request'):
            func_name = func_name[:-8]

        request.parsed_data.xml = xml
        request.parsed_data.func_xml = func
        request.parsed_data.func_name = func_name

        return request.parsed_data

    def get_function(self, request):

        if request.tail == 'wsdl':
            return self.get_wsdl

        return self.parse_request(request).func_name

    def get_arguments(self, request, arguments):
        return xml2obj(self.parse_request(request).func_xml, arguments) or {}

    def get_response(self, result, name, return_type):

        result = obj2xml(
            et.Element(name + '_response', namespace=self.tns),
            {'result': result},
            TypeFactory({DICT_NAME_KEY: 'fake', 'result': return_type}))

        body = et.Element(soap_env_name('Body'), nsmap=self.namespaces)
        body.append(result)

        xml = et.Element(soap_env_name('Envelope'), nsmap=self.namespaces)
        xml.append(body)

        return Response(et.tostring(xml,
            encoding=ENCODING, pretty_print=True, xml_declaration=True))

    def get_error_response(self, error):

        error = self.get_error(error)

        fault = et.Element(soap_env_name('Fault'), nsmap=self.namespaces)
        faultcode = et.SubElement(fault, 'faultcode')
        faultcode.text = 'se:%s' % error['type']
        faultstring = et.SubElement(fault, 'faultstring')
        faultstring.text = error['message']
        error['exceptionName'] = \
            get_axis_package_name(types_ns(self.tns)) + '.Error'
        fault.append(obj2xml(et.Element('detail'), error))

        body = et.Element(soap_env_name('Body'), nsmap=self.namespaces)
        body.append(fault)

        xml = et.Element(soap_env_name('Envelope'), nsmap=self.namespaces)
        xml.append(body)

        return Response(et.tostring(xml,
            encoding=ENCODING, pretty_print=True, xml_declaration=True))

    #noinspection PyUnusedLocal
    def get_wsdl(self, server, request):
        headers_schema = getattr(self.auth_data_getter, 'headers_schema', None)
        return Response(
            WsdlGenerator(
                server, self.service_name, self.tns, self.location,
                headers_schema, ENCODING).get_wsdl())