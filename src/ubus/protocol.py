import requests
import json
import logging
from pprint import pformat as pf, pprint as pp
from enum import IntEnum
from typing import List, Dict

_LOGGER = logging.getLogger(__name__)

class UbusError(IntEnum):
    """ from ubusmsg.h
    GNU Lesser General Public License version 2.1
    """
    UBUS_STATUS_OK = 0,
    UBUS_STATUS_INVALID_COMMAND = 1,
    UBUS_STATUS_INVALID_ARGUMENT = 2,
    UBUS_STATUS_METHOD_NOT_FOUND = 3,
    UBUS_STATUS_NOT_FOUND = 4,
    UBUS_STATUS_NO_DATA = 5,
    UBUS_STATUS_PERMISSION_DENIED = 6,
    UBUS_STATUS_TIMEOUT = 7,
    UBUS_STATUS_NOT_SUPPORTED = 8,
    UBUS_STATUS_UNKNOWN_ERROR = 9,
    UBUS_STATUS_CONNECTION_FAILED = 10,
    __UBUS_STATUS_LAST = 11


class UbusException(Exception):
    pass

class Method:
    def __init__(self, ubus, namespace, name, parameters):
        self.ubus = ubus
        self.namespace = namespace
        self.name = name
        self.parameters = self._parse_parameters(parameters)

    def _parse_parameters(self, parameters):
        if "ubus_rpc_session" in parameters:
            del parameters["ubus_rpc_session"]
        return parameters

    def has_access(self):
        raise NotImplementedError()
        return self.ubus._do_request("call", "session", "access", {'scope': 'ubus', 'object': self.namespace.name, 'method': self.name})

    def __call__(self, *args, **kwargs):
        for kw in kwargs:
            if kw not in self.parameters.keys():
                raise UbusException("Got unwanted parameter '%s'" % kw)
        keys = self.parameters.keys()
        args = {k: kwargs[k] for k in keys if k in kwargs}

        if len(args) > len(self.parameters):
            raise UbusException("Got too many parameters for %s. Got %s, wanted %s" % (self, len(args), len(self.parameters)))

        return self.namespace.call(self.name, **args)

    def __str__(self):
        return "<UbusMethod %s %s (%s)>" % (self.namespace, self.name, self.parameters)

    def __repr__(self):
        return self.__str__()

class UbusNamespace:
    def __init__(self, ubus, name):
        self.ubus = ubus
        self.name = name
        self._methods = None

    def __iter__(self):
        yield from self.methods

    @property
    def methods(self) -> List[Method]:
        if not self._methods:
            self._methods = self._fetch_methods()

        return self._methods

    def call(self, method, **kwargs):
        #_LOGGER.debug("Calling %s with kwargs %s" % (method, kwargs))
        return self.ubus._do_request('call', self.name, method, **kwargs)

    def __getitem__(self, item) -> Method:
        if item not in self.methods:
            raise UbusException("Tried to access non-existing method %s" % item)
        return self.methods[item]

    def _fetch_methods(self) -> List[Method]:
        #_LOGGER.info("Fetching methods for %s" % self.name)
        res = self.ubus._do_request('list', self.name, '')
        if self.name not in res:
            raise UbusException("Got incorrect method listing, wanted for %s, got %s" % (self.name, res))

        methods = res[self.name]
        methods = {x: Method(self.ubus, self, x, methods[x]) for x in methods}

        return methods

    def __repr__(self):
        return "<UbusNamespace: %s>" % self.name


class Ubus:
    def __init__(self, host, username=None, password=None):
        self.endpoint = 'http://%s/ubus' % host
        self.username = username
        self.password = password
        self.timeout = 5
        self._ifaces = None
        self._message_id = 0
        self._session_id = "00000000000000000000000000000000"
        _LOGGER.debug("Using %s with username %s", self.endpoint, self.username)

    @property
    def id(self):
        self._message_id += 1
        return self._message_id

    def login(self, username=None, password=None):
        result = self["session"]["login"](username=username, password=password)
        if "ubus_rpc_session" not in result:
            raise UbusException("Login failed, got no ubus_rpc_session: %s" % result)
        self._session_id = result["ubus_rpc_session"]
        return result

    def __enter__(self):
        result = self.login(self.username, self.password)
        #{'acls': {'access-group': {'hass': ['read'],
        #                           'unauthenticated': ['read']},
        #          'ubus': {'dhcp': ['ipv4leases', 'ipv6leases'],
        #                   'iwinfo': ['devices', 'assoclist'],
        #                   'session': ['access', 'login']}},
        # 'data': {'username': 'hass'},
        # 'expires': 300,
        # 'timeout': 300,
        # 'ubus_rpc_session': '7099de950604de9f358d16fc8e8e4950'}]}
        #print(result)

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @property
    def session_id(self):
        return self._session_id

    def _do_request(self, rpcmethod, subsystem, method, **params):
        if len(params) == 0:
            params = {}
        data = json.dumps({"jsonrpc": "2.0",
                           "id": self.id,
                           "method": rpcmethod,
                           "params": [self.session_id,
                                      subsystem,
                                      method,
                                      params]})
        _LOGGER.debug(">> %s" % pf(data))

        try:
            res = requests.post(self.endpoint, data=data, timeout=self.timeout)
        except requests.exceptions.Timeout as ex:
            raise UbusException("Got timeout") from ex
        except requests.exceptions.ConnectionError as ex:
            raise UbusException("Got error during post, false credentials?") from ex

        if res.status_code == 200:
            response = res.json()
            _LOGGER.debug("<< %s" % pf(response))

            if 'error' in response:
                raise UbusException("Got error from ubus: %s" % response['error'])

            if 'result' not in response:
                raise UbusException("Got no result: %s" % response)

            result = response["result"]
            if isinstance(result, dict): # got payload, passing directly
                return result
            if isinstance(result, list): # got list, first one for error code, second for payload
                if len(result) == 1:
                    error = UbusError(result[0])
                    raise UbusException("Got error %s" % error)
                if len(result) != 2:
                    raise UbusException("Result length was not 2: %s" % result)

                error = UbusError(result[0])
                if error != UbusError.UBUS_STATUS_OK:
                    raise UbusException("Got an error: %s" % result)

                payload = result[1]
                if method in payload: # unwrap if necessary
                    payload = payload[method]
                if isinstance(payload, bool): # e.g. access() returns a boolean
                    return payload
                if 'results' in payload:
                    payload = payload['results']
                return payload
        else:
            raise UbusException("Got a non-200 retcode: %s" % res.status_code)

    @property
    def namespaces(self) -> Dict[str, UbusNamespace]:
        if not self._ifaces:
            self._ifaces = self._fetch_ifaces()
        return self._ifaces

    def _fetch_ifaces(self) -> Dict[str, UbusNamespace]:
        ifaces = {x: UbusNamespace(self, x) for x in self._do_request('list', '*', '')}
        _LOGGER.debug("Got %s interfaces" % len(ifaces))
        return ifaces

    def __iter__(self):
        yield from self.namespaces.values()

    def __getitem__(self, item) -> UbusNamespace:
        if item not in self.namespaces:
            raise UbusException("Tried to access non-existing interface %s" % item)
        return self.namespaces[item]

    def __str__(self):
        return "<Ubus %s: %s interfaces, sid: %s>" % (self.endpoint, len(self.namespaces), self._session_id)
