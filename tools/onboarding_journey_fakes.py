"""External-boundary doubles for installed public-CLI onboarding journeys.

No business-level onboarding function is replaced. Modal public methods use this
RPC stub; native login uses an executable synthetic issuer in a private directory.
"""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace


class SyntheticS3:
    def __init__(self, events, objects, metadata=None, **kwargs):
        self.events, self.objects = events, objects
        self.metadata = metadata if metadata is not None else {}
        self.meta = SimpleNamespace(
            endpoint_url=kwargs.get("endpoint_url", "https://t3.storage.dev"),
            config=kwargs["config"],
        )
        self.fail_reads = False

    def _event(self, name, kwargs):
        from botocore.exceptions import ClientError

        self.events.append({"transport": "s3", "operation": name})
        if self.fail_reads:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "synthetic-provider-error",
                    }
                },
                name,
            )

    def get_bucket_location(self, **kwargs):
        self._event("GetBucketLocation", kwargs)
        return {
            "LocationConstraint": "iad",
            "ResponseMetadata": {
                "HTTPStatusCode": 200,
                "HTTPHeaders": {"x-tigris-bucket-location-type": "single"},
            },
        }

    def head_bucket(self, **kwargs):
        self._event("HeadBucket", kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def list_objects_v2(self, **kwargs):
        self._event("ListObjectsV2", kwargs)
        return {
            "IsTruncated": False,
            "Contents": [
                {"Key": key, "Size": len(body)}
                for (bucket, key), body in self.objects.items()
                if bucket == kwargs["Bucket"]
                and key.startswith(kwargs.get("Prefix", ""))
            ][: kwargs.get("MaxKeys", 1000)],
        }

    def get_bucket_policy_status(self, **kwargs):
        self._event("GetBucketPolicyStatus", kwargs)
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "PolicyStatus": {"IsPublic": False},
        }

    def get_bucket_acl(self, **kwargs):
        self._event("GetBucketAcl", kwargs)
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "Owner": {"ID": "synthetic-owner"},
            "Grants": [
                {
                    "Grantee": {"Type": "CanonicalUser", "ID": "synthetic-owner"},
                    "Permission": "FULL_CONTROL",
                }
            ],
        }

    def get_object_acl(self, **kwargs):
        self._event("GetObjectAcl", kwargs)
        self._body(kwargs)
        return self.get_bucket_acl(Bucket=kwargs["Bucket"])

    def _body(self, kwargs):
        from botocore.exceptions import ClientError

        try:
            return self.objects[kwargs["Bucket"], kwargs["Key"]]
        except KeyError:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            ) from None

    def get_object(self, **kwargs):
        self._event("GetObject", kwargs)
        body = self._body(kwargs)
        return {
            "Body": io.BytesIO(body),
            "ETag": hashlib.sha256(body).hexdigest(),
            "ContentLength": len(body),
            **self.metadata.get((kwargs["Bucket"], kwargs["Key"]), {}),
        }

    def head_object(self, **kwargs):
        self._event("HeadObject", kwargs)
        body = self._body(kwargs)
        return {
            "ETag": hashlib.sha256(body).hexdigest(),
            "ContentLength": len(body),
            "Metadata": {"sha256": hashlib.sha256(body).hexdigest()},
            **self.metadata.get((kwargs["Bucket"], kwargs["Key"]), {}),
        }

    def put_object(self, **kwargs):
        from botocore.exceptions import ClientError

        self._event("PutObject", kwargs)
        key = kwargs["Bucket"], kwargs["Key"]
        old = self.objects.get(key)
        if (kwargs.get("IfNoneMatch") == "*" and old is not None) or (
            "IfMatch" in kwargs
            and (old is None or kwargs["IfMatch"] != hashlib.sha256(old).hexdigest())
        ):
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        body = kwargs["Body"]
        if hasattr(body, "read"):
            body = body.read()
        self.objects[key] = body
        self.metadata[key] = {
            name: kwargs[name]
            for name in (
                "Metadata",
                "ContentType",
                "ChecksumSHA256",
                "ServerSideEncryption",
            )
            if name in kwargs
        }
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ETag": hashlib.sha256(body).hexdigest(),
        }


class ModalRpc:
    """Requests are constructed by installed Modal 1.5.4, not by this double."""

    def __init__(self, events):
        self.events = events
        self.environments = {}
        self.secrets = {}
        self.secret_ids = {}
        self.requests = []
        self.functions = set()
        self.fail = None

    def _record(self, name, request):
        from google.protobuf.message import Message

        if not isinstance(request, Message):
            raise TypeError("expected an actual SDK protobuf request")
        self.requests.append(request)
        event = {
            "transport": "modal",
            "operation": name,
            "request_type": type(request).__name__,
            "fields": sorted(field.name for field, _value in request.ListFields()),
        }
        if name == "SecretGetOrCreate":
            event.update(
                environment=request.environment_name,
                creation_type=request.object_creation_type,
                selected_names=sorted(request.env_dict),
            )
        elif name == "SecretUpdate":
            event["selected_names"] = sorted(item.key for item in request.updates)
        elif name == "EnvironmentCreate":
            event["environment"] = request.name
        elif name == "FunctionMap":
            event["input_count"] = len(request.pipelined_inputs)
        self.events.append(event)
        if self.fail == name:
            raise RuntimeError("synthetic-provider-error")

    async def EnvironmentGetOrCreate(self, request, **kwargs):
        from modal.exception import NotFoundError
        from modal_proto import api_pb2

        self._record("EnvironmentGetOrCreate", request)
        if request.deployment_name not in self.environments:
            raise NotFoundError("synthetic missing environment")
        return api_pb2.EnvironmentGetOrCreateResponse(
            environment_id=self.environments[request.deployment_name]
        )

    async def EnvironmentCreate(self, request, **kwargs):
        self._record("EnvironmentCreate", request)
        self.environments[request.name] = "en-synthetic"

    async def SecretGetOrCreate(self, request, **kwargs):
        from modal.exception import AlreadyExistsError, NotFoundError
        from modal_proto import api_pb2

        self._record("SecretGetOrCreate", request)
        key = request.environment_name, request.deployment_name
        create = (
            request.object_creation_type
            == api_pb2.OBJECT_CREATION_TYPE_CREATE_FAIL_IF_EXISTS
        )
        if create:
            if key in self.secrets:
                raise AlreadyExistsError("synthetic existing secret")
            self.secrets[key] = dict(request.env_dict)
            self.secret_ids[key] = f"st-synthetic-{len(self.secrets)}"
        elif key not in self.secrets:
            raise NotFoundError("synthetic missing secret")
        return api_pb2.SecretGetOrCreateResponse(
            secret_id=self.secret_ids[key], metadata=api_pb2.SecretMetadata(name=key[1])
        )

    async def SecretUpdate(self, request, **kwargs):
        self._record("SecretUpdate", request)
        key = next(
            k for k, value in self.secret_ids.items() if value == request.secret_id
        )
        self.secrets[key].update({item.key: item.value for item in request.updates})

    async def FunctionGet(self, request, **kwargs):
        from modal.exception import NotFoundError
        from modal_proto import api_pb2

        self._record("FunctionGet", request)
        if (request.app_name, request.environment_name) not in self.functions:
            raise NotFoundError("synthetic function not deployed")
        return api_pb2.FunctionGetResponse(
            function_id="fu-synthetic",
            handle_metadata=api_pb2.FunctionHandleMetadata(function_name="controller"),
        )

    async def FunctionMap(self, request, **kwargs):
        from modal_proto import api_pb2

        self._record("FunctionMap", request)
        if len(request.pipelined_inputs) != 1:
            raise ValueError("journey expects one SDK input")
        return api_pb2.FunctionMapResponse(
            function_call_id="fc-synthetic-submission",
            pipelined_inputs=[
                api_pb2.FunctionPutInputsResponseItem(input_id="in-synthetic")
            ],
        )


# The fake binary is a real child process. It receives no real credentials, and
# the only login state it issues is synthetic native-format data in its own HOME.
ISSUER = r"""#!/usr/bin/env python3
import base64,json,os,pathlib,sys,time,uuid
args=sys.argv[1:]
home=pathlib.Path(os.environ['CODEX_HOME'])
auth=home/'auth.json'
if args==['--version']:
    print('codex-cli 0.154.0');raise SystemExit(0)
if 'logout' in args:
    auth.unlink(missing_ok=True);raise SystemExit(0)
if 'login' in args and 'status' not in args:
    if '--with-api-key' in args:
        value={'auth_mode':'apikey','OPENAI_API_KEY':sys.stdin.read().strip()}
    else:
        nonce=uuid.uuid4().hex
        claims=base64.urlsafe_b64encode(json.dumps({'exp':int(time.time())+86400}).encode()).decode().rstrip('=')
        value={'auth_mode':'chatgpt','OPENAI_API_KEY':None,'tokens':{'access_token':'e30.'+claims+'.'+nonce,
               'id_token':'synthetic-id-'+nonce,'refresh_token':'synthetic-refresh-'+nonce}}
    home.mkdir(parents=True,exist_ok=True);auth.write_text(json.dumps(value));auth.chmod(0o600)
    raise SystemExit(0)
if args[-2:]==['login','status']:
    value=json.loads(auth.read_bytes())
    status = ('Logged in using an API key - ***' if value.get('OPENAI_API_KEY')
              else 'Logged in using ChatGPT')
    print(status,file=sys.stderr)
    raise SystemExit(0)
if args[-1:] == ['app-server']:
    if (pathlib.Path(__file__).parent/'fail-provider').exists():
        value=json.loads(auth.read_bytes())
        message='synthetic-provider-error '+str(value.get('OPENAI_API_KEY',''))
        print(message,file=sys.stderr)
        raise SystemExit(1)
    import tomllib
    config_path=home/'config.toml'
    config=tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    for line in sys.stdin:
        q=json.loads(line);method=q['method']
        if method=='initialized':continue
        if method=='initialize':result={'userAgent':'codex-cli 0.154.0'}
        elif method=='config/read':
            result={'config':{'model_provider':'openai',**config}}
        elif method=='model/list':result={'data':[
             {'id':'gpt-6-astra','model':'gpt-6-astra',
             'defaultReasoningEffort':'low','supportedReasoningEfforts':[
             {'reasoningEffort':'low','description':'synthetic'},
             {'reasoningEffort':'high','description':'synthetic'}]}],'nextCursor':None}
        else:raise SystemExit(78)
        print(json.dumps({'id':q['id'],'result':result}),flush=True)
    raise SystemExit(0)
raise SystemExit(79)
"""
