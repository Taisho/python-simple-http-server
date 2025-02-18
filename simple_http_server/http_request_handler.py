# -*- coding: utf-8 -*-

"""
Copyright (c) 2018 Keijack Wu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


import asyncio
from asyncio.streams import StreamReader
import os
import json
import inspect
import threading
import http.cookies as cookies
import datetime

from urllib.parse import unquote
from typing import Any, Callable, Dict, List, Tuple, Union

from simple_http_server.models.model_bindings import ModelBindingConf

from .models.basic_models import FilterContext, ModelDict, Environment, RegGroup, RegGroups, HttpError, RequestBodyReader, StaticFile, \
    Headers, Redirect, Response, Cookies, Cookie, JSONBody, BytesBody, Header, Parameters, PathValue, \
    Parameter, MultipartFile, Request, Session, SessionFactory, DEFAULT_ENCODING, SESSION_COOKIE_NAME
from .app_conf import ControllerFunction
import simple_http_server.__utils as utils

from ._http_session_local_impl import LocalSessionFactory
from .logger import get_logger
from .__utils import get_function_args, get_function_kwargs

_logger = get_logger("simple_http_server.http_request_handler")


class RequestBodyReaderWrapper(RequestBodyReader):

    def __init__(self, reader: StreamReader, content_length: int = None) -> None:
        self._content_length: int = content_length
        self._remain_length: int = content_length
        self._reader: StreamReader = reader

    async def read(self, n: int = -1):
        data = b''
        if self._remain_length is not None and self._remain_length <= 0:
            return data
        if not n or n < 0:
            if self._remain_length:
                data = await self._reader.read(self._remain_length)
            else:
                data = await self._reader.read()
        else:
            if self._remain_length and n > self._remain_length:
                data = await self._reader.read(self._remain_length)
            else:
                data = await self._reader.read(n)

        if self._remain_length and self._remain_length > 0:
            self._remain_length -= len(data)

        return data


class RequestWrapper(Request):

    def __init__(self):
        super().__init__()
        self._headers_keys_in_lowcase = {}
        self._path = ""
        self.__session = None
        self._socket_req = None
        self._coroutine_objects = []
        self._session_fac: SessionFactory = None

    @property
    def host(self) -> str:
        if "host" not in self._headers_keys_in_lowcase:
            return ""
        else:
            return self.headers["host"]

    @property
    def content_type(self) -> str:
        if "content-type" not in self._headers_keys_in_lowcase:
            return ""
        else:
            return self.headers["content-type"]

    @property
    def content_length(self) -> int:
        if "content-length" not in self._headers_keys_in_lowcase:
            return ""
        else:
            return self.headers["content-length"]

    def get_session(self, create: bool = False) -> Session:
        if not self.__session:
            sid = self.cookies[SESSION_COOKIE_NAME].value if SESSION_COOKIE_NAME in self.cookies.keys(
            ) else ""
            session_fac = self._session_fac or LocalSessionFactory()
            self.__session = session_fac.get_session(sid, create)
        return self.__session

    def _put_coroutine_task(self, coroutine_object):
        self._coroutine_objects.append(coroutine_object)


class ResponseWrapper(Response):
    """ """

    def __init__(self, handler,
                 status_code=200,
                 headers=None):
        super().__init__(status_code=status_code, headers=headers, body="")
        self.__req_handler = handler
        self.__is_sent = False
        self.__header_sent = False
        self.__send_lock__ = threading.Lock()

    @property
    def is_sent(self) -> bool:
        return self.__is_sent

    def send_error(self, status_code: int, message: str = "", explain: str = "") -> None:
        with self.__send_lock__:
            self.__is_sent = True
            self.status_code = status_code
            self.__req_handler.send_error(
                self.status_code, message=message, explain=explain, headers=self.headers)

    def send_redirect(self, url: str) -> None:
        self.status_code = 302
        self.set_header("Location", url)
        self.body = None
        self.send_response()

    def send_response(self) -> None:
        with self.__send_lock__:
            self.__send_response()

    def __send_response(self):
        assert not self.__is_sent and not self.__header_sent, "This response has benn sent"
        self.__header_sent = True
        self.__is_sent = True
        self.__req_handler._send_response({
            "status_code": self.status_code,
            "headers": self.headers,
            "cookies": self.cookies,
            "body": self.body
        })

    def __send_headers(self):
        if not self.__header_sent:
            self.__header_sent = True
            self.__req_handler._send_res_headers(
                self.status_code, headers=self.headers, cks=self.cookies)

    def write_bytes(self, data: bytes):
        assert not self.__is_sent, "This response has benn sent"
        assert isinstance(data, bytes) or isinstance(
            data, bytearray), "You can "
        self.__send_headers()
        self.__req_handler.writer.write(data)

    def close(self):
        self.__is_sent = True
        self.__req_handler.writer.write_eof()
        self.__req_handler.writer.close()


class FilterContextImpl(FilterContext):
    """Context of a filter"""

    DEFAULT_TIME_OUT = 10

    def __init__(self, req, res, controller: ControllerFunction, model_binding_conf: ModelBindingConf, filters: List[Callable] = None):
        self.__request: RequestWrapper = req
        self.__response = res
        self.__controller: ControllerFunction = controller
        self.__filters: List[Callable] = filters if filters is not None else []
        self.__model_binding_conf = model_binding_conf

    @property
    def request(self) -> RequestWrapper:
        return self.__request

    @property
    def response(self) -> ResponseWrapper:
        return self.__response

    async def _run_ctrl_fun(self):
        args = await self.__prepare_args()
        kwargs = await self.__prepare_kwargs()
        if asyncio.iscoroutinefunction(self.__controller.func):
            if kwargs is None:
                ctr_res = await self.__controller.func(*args)
            else:
                ctr_res = await self.__controller.func(*args, **kwargs)
        else:
            if kwargs is None:
                ctr_res = self.__controller.func(*args)
            else:
                ctr_res = self.__controller.func(*args, **kwargs)
        return ctr_res

    def _do_res(self, ctr_res):
        session = self.request.get_session()
        if session and session.is_valid:
            exp = datetime.datetime.utcfromtimestamp(
                session.last_accessed_time + session.max_inactive_interval)
            sck = Cookies()
            sck[SESSION_COOKIE_NAME] = session.id
            sck[SESSION_COOKIE_NAME]["httponly"] = True
            sck[SESSION_COOKIE_NAME]["path"] = "/"
            sck[SESSION_COOKIE_NAME]["expires"] = exp.strftime(
                Cookies.EXPIRE_DATE_FORMAT)
            self.response.cookies.update(sck)
        elif session and SESSION_COOKIE_NAME in self.request.cookies:
            exp = datetime.datetime.utcfromtimestamp(0)
            sck = Cookies()
            sck[SESSION_COOKIE_NAME] = session.id
            sck[SESSION_COOKIE_NAME]["httponly"] = True
            sck[SESSION_COOKIE_NAME]["path"] = "/"
            sck[SESSION_COOKIE_NAME]["expires"] = exp.strftime(
                Cookies.EXPIRE_DATE_FORMAT)
            self.response.cookies.update(sck)

        if ctr_res is not None:
            if isinstance(ctr_res, tuple):
                status, headers, cks, body = self.__decode_tuple_response(
                    ctr_res)
                self.response.status_code = status if status is not None else self.response.status_code
                self.response.body = body if body is not None else self.response.body
                self.response.add_headers(headers)
                self.response.cookies.update(cks)
            elif isinstance(ctr_res, Response):
                self.response.status_code = ctr_res.status_code
                self.response.body = ctr_res.body
                self.response.add_headers(ctr_res.headers)
            elif isinstance(ctr_res, Redirect):
                self.response.send_redirect(ctr_res.url)
            elif isinstance(ctr_res, int) and ctr_res >= 200 and ctr_res < 600:
                self.response.status_code = ctr_res
            elif isinstance(ctr_res, Headers):
                self.response.add_headers(ctr_res)
            elif isinstance(ctr_res, cookies.BaseCookie):
                self.response.cookies.update(ctr_res)
            else:
                self.response.body = ctr_res

        if self.request.method.upper() == "HEAD":
            self.response.body = None
        if not self.response.is_sent:
            self.response.send_response()

    async def _do_request_async(self):
        ctr_res = await self._run_ctrl_fun()
        self._do_res(ctr_res)

    def do_chain(self):
        if self.response.is_sent:
            return
        if self.__filters:
            filter_func = self.__filters.pop(0)
            self.request._put_coroutine_task(
                self._wrap_to_async(filter_func, [self]))
        else:
            self.request._put_coroutine_task(self._do_request_async())

    async def _wrap_to_async(self, func: Callable, args: List = [], kwargs: Dict = {}) -> Any:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)

    def __decode_tuple_response(self, ctr_res):
        status_code = None
        headers = Headers()
        cks = cookies.SimpleCookie()
        body = None
        for item in ctr_res:
            if isinstance(item, int):
                if status_code is None:
                    status_code = item
            elif isinstance(item, Headers):
                headers.update(item)
            elif isinstance(item, cookies.BaseCookie):
                cks.update(item)
            elif type(item) in (str, dict, StaticFile, bytes, bytearray):
                if body is None:
                    body = item
        return status_code, headers, cks, body

    async def __prepare_args(self):
        args = get_function_args(self.__controller.func)
        arg_vals = []
        if len(args) > 0:
            ctr_obj = self.__controller.ctrl_object
            if ctr_obj is not None:
                arg_vals.append(self.__controller.ctrl_object)
                args = args[1:]
        for arg, arg_type_anno in args:
            param = await self.__get_params_(arg, arg_type_anno)
            if param is None:
                raise HttpError(400, "Missing Paramter",
                                f"Parameter[{arg}] is required! ")
            arg_vals.append(param)

        return arg_vals

    async def __get_params_(self, arg, arg_type, val=None):
        if arg_type in self.__model_binding_conf.model_bingding_types:
            binding_type = self.__model_binding_conf.model_bingding_types[arg_type]
        else:
            binding_type = self.__model_binding_conf.default_model_binding_type

        binding_obj = binding_type(self.request,
                                   self.response,
                                   arg,
                                   arg_type,
                                   val)
        return await self._wrap_to_async(binding_obj.bind)

    async def __prepare_kwargs(self):
        kwargs = get_function_kwargs(self.__controller.func)
        if kwargs is None:
            return None
        kwarg_vals = {}
        for k, v, t in kwargs:
            kwarg_vals[k] = await self.__get_params_(
                k, type(v) if v is not None else t, v)

        return kwarg_vals


class HTTPRequestHandler:

    def __init__(self, http_protocol_handler, environment={}) -> None:
        self.method: str = http_protocol_handler.command
        self.request_path: str = http_protocol_handler.request_path
        self.query_string: str = http_protocol_handler.query_string
        self.query_parameters: Dict[str, List[str]
                                    ] = http_protocol_handler.query_parameters
        self.headers: Dict[str, Dict[str, str]] = http_protocol_handler.headers

        self.routing_conf = http_protocol_handler.routing_conf
        self.reader = http_protocol_handler.reader

        self.send_header = http_protocol_handler.send_header
        self.end_headers = http_protocol_handler.end_headers
        self.send_response = http_protocol_handler.send_response
        self.send_error = http_protocol_handler.send_error
        self.writer = http_protocol_handler.writer
        self.environment: Dict[str, Any] = environment

    def __match_one_exp(self, d: Dict[str, Union[List[str], str]], exp: str, where: str) -> bool:
        if not exp:
            return True
        _logger.debug(
            f"Match controller {where} expression [{exp}] for values: {d}. ")
        exp_ = str(exp)
        e_idx = exp_.find("=")
        if e_idx < 0:
            _logger.debug(f"cannot find = in exp {exp}")
            if exp_.startswith('!'):
                return not exp_[1:] in d.keys()
            else:
                return exp_ in d.keys()
        idx = exp_.find("!=")
        if idx > 0 and idx < e_idx:
            k, v = utils.break_into(exp_, '!=')
            if k not in d.keys():
                return False
            dvals = d[k]
            if isinstance(dvals, str):
                dvals = [dvals]
            for dv in dvals:
                if dv == v:
                    return False
            return True

        idx = exp_.find("^=")
        if idx > 0 and idx < e_idx:
            k, v = utils.break_into(exp_, "^=")
            if k not in d.keys():
                return False
            dvals = d[k]
            if isinstance(dvals, str):
                dvals = [dvals]
            for dv in dvals:
                _logger.debug(f"^= mapping:: {dv} ... {v}")
                if dv.startswith(v):
                    return True
            return False

        if e_idx > 0:
            k, v = utils.break_into(exp_, '=')
            if k not in d.keys():
                return False
            dvals = d[k]
            if isinstance(dvals, str):
                dvals = [dvals]
            for dv in dvals:
                if dv == v:
                    return True
            return False

        _logger.error(
            f"Controller {where} expression [{exp}] is not valied, returning False...")
        return False

    def __match_exps(self, d: Dict[str, Union[List[str], str]], exps: List[str], all: bool, where: str) -> bool:
        if not exps:
            return True

        for exp in exps:
            res = self.__match_one_exp(d, exp, where)
            if all and not res:
                return False
            if not all and res:  # match one
                return True
        # return if all True else False
        return all

    def __is_req_match_ctl(self, req: RequestWrapper, ctrl: ControllerFunction) -> bool:
        _logger.debug(f"{ctrl.headers} - {ctrl.params}")
        return self.__match_exps(req.headers, ctrl.headers, ctrl.match_all_headers_expressions, 'headers') \
            and self.__match_exps(req.parameters, ctrl.params, ctrl.match_all_params_expressions, 'params')

    def __get_ctrl(self, req: RequestWrapper) -> Tuple[ControllerFunction, Dict, List]:
        mth = self.method.upper()
        path = req._path
        ctrls = self.routing_conf.get_url_controllers(path, mth)
        for ctrl, pvs, regs in ctrls:
            if self.__is_req_match_ctl(req, ctrl):
                return ctrl, pvs, regs
        return None, {}, []

    async def handle_request(self):
        mth = self.method.upper()

        req = await self.__prepare_request(mth)

        ctrl, req.path_values, req.reg_groups = self.__get_ctrl(req)

        res = ResponseWrapper(self)
        if ctrl is None:
            res.send_error(404, "Controller Not Found",
                           "Cannot find a controller for your path")
        else:
            filters = self.routing_conf.get_matched_filters(req.path)
            ctx = FilterContextImpl(
                req, res, ctrl, self.routing_conf.model_binding_conf, filters)
            try:
                ctx.do_chain()
                if req._coroutine_objects:
                    _logger.debug(f"wait all the objects in waiting list.")
                    while req._coroutine_objects:
                        await req._coroutine_objects.pop(0)
            except HttpError as e:
                res.send_error(e.code, e.message, e.explain)
            except Exception as e:
                _logger.exception("error occurs! returning 500")
                res.send_error(500, None, str(e))

    async def __prepare_request(self, method) -> RequestWrapper:
        path = self.request_path
        req = RequestWrapper()
        req.environment = self.environment or {}
        req.path = "/" + path
        req._path = path
        req._session_fac = self.routing_conf.session_factory
        headers = {}
        _headers_keys_in_lowers = {}
        for k in self.headers.keys():
            headers[k] = self.headers[k]
            _headers_keys_in_lowers[k.lower()] = self.headers[k]
        req.headers = headers
        req._headers_keys_in_lowcase = _headers_keys_in_lowers

        # cookies
        if "cookie" in _headers_keys_in_lowers.keys():
            req.cookies.load(_headers_keys_in_lowers["cookie"])

        req.method = method

        req.parameters = self.query_parameters

        if "content-length" in _headers_keys_in_lowers.keys():
            content_length = int(_headers_keys_in_lowers["content-length"])

            req.reader = RequestBodyReaderWrapper(self.reader, content_length)

            content_type = _headers_keys_in_lowers["content-type"]
            if content_type.lower().startswith("application/x-www-form-urlencoded"):
                req._body = await req.reader.read(content_length)
                data_params = utils.decode_query_string(
                    req._body.decode(DEFAULT_ENCODING))
            elif content_type.lower().startswith("multipart/form-data"):
                req._body = await req.reader.read(content_length)
                data_params = self.__decode_multipart(
                    content_type, req._body.decode("ISO-8859-1"))
            elif content_type.lower().startswith("application/json"):
                req._body = await req.reader.read(content_length)
                req.json = json.loads(req._body.decode(DEFAULT_ENCODING))
                data_params = {}
            else:
                data_params = {}
            req.parameters = self.__merge(data_params, req.parameters)
        else:
            req.reader = RequestBodyReaderWrapper(self.reader)
        return req

    def __merge(self, dic0: Dict[str, List[str]], dic1: Dict[str, List[str]]):
        """Merge tow dictionaries of which the structure is {k:[v1, v2]}"""
        dic = dic0
        for k, v in dic1.items():
            if k not in dic.keys():
                dic[k] = v
            else:
                for i in v:
                    dic[k].append(i)
        return dic

    def __decode_multipart(self, content_type, data):
        boundary = "--" + content_type.split("; ")[1].split("=")[1]
        fields = data.split(boundary)
        # ignore the first empty row and the last end symbol
        fields = fields[1: len(fields) - 1]
        params = {}
        for field in fields:
            # trim the first and the last empty row
            f = field[field.index("\r\n") + 2: field.rindex("\r\n")]
            key, val = self.__decode_multipart_field(f)
            utils.put_to(params, key, val)
        return params

    def __decode_multipart_field(self, field):
        # first line: Content-Disposition
        line, rest = self.__read_line(field)

        kvs = self.__decode_content_disposition(line)
        kname = kvs["name"].encode(
            "ISO-8859-1", errors="replace").decode(DEFAULT_ENCODING, errors="replace")
        if len(kvs) == 1:
            # this is a string field, the second line is an empty line, the rest is the value
            val = self.__read_line(rest)[1].encode(
                "ISO-8859-1", errors="replace").decode(DEFAULT_ENCODING, errors="replace")
        elif "filename" in kvs or "filename*" in kvs:
            if "filename*" in kvs:
                name_value = kvs["filename*"]
                idx = name_value.find("'")
                if idx <= 0:
                    encoding = DEFAULT_ENCODING
                else:
                    encoding = name_value[0:idx]
                name_value = name_value[idx + 1:]
                idx = name_value.find("'")
                filename = unquote(name_value[idx + 1:], encoding)
            else:
                filename = kvs["filename"].encode(
                    "ISO-8859-1", errors="replace").decode(DEFAULT_ENCODING, errors="replace")

            # the second line is Content-Type line
            ct_line, rest = self.__read_line(rest)
            content_type = ct_line.split(":")[1].strip()
            # the third line is an empty line, the rest is the value
            content = self.__read_line(rest)[1].encode(
                "ISO-8859-1", errors="replace")

            val = MultipartFile(kname, filename=filename,
                                content_type=content_type, content=content)
        else:
            val = "UNKNOWN"

        return kname, val

    def __decode_content_disposition(self, line) -> Dict[str, str]:
        cont_dis = {}
        es = line.split(";")[1:]
        for e in es:
            k, v = utils.break_into(e.strip(), "=")
            if v.startswith('"') and v.endswith('"'):
                cont_dis[k] = v[1: -1]  # ignore the '"' symbol
            else:
                cont_dis[k] = v
        return cont_dis

    def __read_line(self, txt):
        return utils.break_into(txt, "\r\n")

    def _send_response(self, response):
        try:
            headers = response["headers"]
            cks = response["cookies"]
            raw_body = response["body"]
            status_code = response["status_code"]
            content_type, body = utils.decode_response_body(raw_body)

            self._send_res(status_code, headers, content_type, cks, body)

        except HttpError as e:
            self.send_error(e.code, e.message, e.explain)

    def __send_res_headers(self, status_code: int, headers: Dict[str, str] = {}, content_type: str = "", cks: Cookies = Cookies()):
        if "Content-Type" not in headers.keys() and "content-type" not in headers.keys():
            headers["Content-Type"] = content_type

        self.send_response(status_code)
        self.send_header("Last-Modified", str(utils.date_time_string()))
        for k, v in headers.items():
            if isinstance(v, str):
                self.send_header(k, v)
            elif isinstance(v, list):
                for iov in v:
                    if isinstance(iov, str):
                        self.send_header(k, iov)

        for k in cks:
            ck = cks[k]
            self.send_header("Set-Cookie", ck.OutputString())

    def _send_res_headers(self, *args, **kwargs):
        """
        " For response object to send and writer headers.
        """
        self.__send_res_headers(*args, **kwargs)
        self.end_headers()

    def _send_res(self, status_code: int, headers: Dict[str, str] = {}, content_type: str = "", cks: Cookies = Cookies(), body: Union[str, bytes, bytearray, StaticFile] = None):
        self.__send_res_headers(status_code, headers, content_type, cks)

        if body is None:
            self.send_header("Content-Length", 0)
            self.end_headers()
        elif isinstance(body, str):
            data = body.encode(DEFAULT_ENCODING, errors="replace")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.writer.write(data)

        elif isinstance(body, bytes) or isinstance(body, bytearray):
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.writer.write(body)
        elif isinstance(body, StaticFile):
            file_size = os.path.getsize(body.file_path)
            self.send_header("Content-Length", file_size)
            self.end_headers()
            buffer_size = 1024 * 1024  # 1M
            with open(body.file_path, "rb") as in_file:
                data = in_file.read(buffer_size)
                while data:
                    self.writer.write(data)
                    data = in_file.read(buffer_size)
