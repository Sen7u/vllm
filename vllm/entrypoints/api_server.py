# SPDX-License-Identifier: Apache-2.0
"""
NOTE: This API server is used only for demonstrating usage of AsyncEngine
and simple performance benchmarks. It is not intended for production use.
For production use, we recommend using our OpenAI compatible server.
We are also not going to accept PRs modifying this file, please
change `vllm/entrypoints/openai/api_server.py` instead.
"""
import asyncio
import json
import ssl
from argparse import Namespace
from dataclasses import asdict
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.entrypoints.launcher import serve_http
from vllm.inputs import TokensPrompt
from vllm.entrypoints.utils import with_cancellation
from vllm.logger import init_logger
from vllm.sampling_params import SamplingParams
from vllm.usage.usage_lib import UsageContext
from vllm.utils import FlexibleArgumentParser, random_uuid, set_ulimit
from vllm.version import __version__ as VLLM_VERSION

logger = init_logger("vllm.entrypoints.api_server")

TIMEOUT_KEEP_ALIVE = 5  # seconds.
app = FastAPI()
engine = None


@app.get("/health")
async def health() -> Response:
    """Health check."""
    return Response(status_code=200)


@app.post("/generate")
async def generate(request: Request) -> Response:
    """Generate completion for the request.

    The request should be a JSON object with the following fields:
    - prompt: the prompt to use for the generation.
    - stream: whether to stream the results or not.
    - other fields: the sampling parameters (See `SamplingParams` for details).
    """
    request_dict = await request.json()
    return await _generate(request_dict, raw_request=request)


@with_cancellation
async def _generate(request_dict: dict, raw_request: Request) -> Response:
    prompt = request_dict.pop("prompt")
    stream = request_dict.pop("stream", False)
    sampling_params = SamplingParams(**request_dict)
    request_id = random_uuid()

    assert engine is not None
    results_generator = engine.generate(prompt, sampling_params, request_id)
    # jimpang add
    inputs = prompt
    if prompt and len(prompt) > 0:
        first_element = prompt[0]
        if isinstance(first_element, int):
            inputs = TokensPrompt(prompt_token_ids=prompt)

    results_generator = engine.generate(
        inputs=inputs, sampling_params=sampling_params, request_id=request_id)

    # Streaming case
    async def stream_results() -> AsyncGenerator[bytes, None]:
        async for request_output in results_generator:
            text_outputs = [
                output.text for output in request_output.outputs
            ]
            output_tokens = [output.token_ids for output in request_output.outputs]
            logprobs = [[{k: asdict(v) for k, v in logprobs.items()} for logprobs in
                         output.logprobs] if output.logprobs is not None else None for output in request_output.outputs]
            ret = {"text": text_outputs, "output_token_ids": output_tokens, "logprobs": logprobs}
            yield (json.dumps(ret) + "\n").encode("utf-8")

    if stream:
        return StreamingResponse(stream_results())

    # Non-streaming case
    final_output = None
    try:
        async for request_output in results_generator:
            final_output = request_output
    except asyncio.CancelledError:
        return Response(status_code=499)

    assert final_output is not None
    text_outputs = [output.text for output in final_output.outputs]
    output_tokens = [output.token_ids for output in final_output.outputs]
    logprobs = [[{k: asdict(v) for k, v in logprobs.items()} for logprobs in
                 output.logprobs] if output.logprobs is not None else None for output in final_output.outputs]
    ret = {"text": text_outputs, "output_token_ids": output_tokens, "logprobs": logprobs}
    return JSONResponse(ret)


def build_app(args: Namespace) -> FastAPI:
    global app

    app.root_path = args.root_path
    return app


async def init_app(
        args: Namespace,
        llm_engine: Optional[AsyncLLMEngine] = None,
) -> FastAPI:
    app = build_app(args)

    global engine

    engine_args = AsyncEngineArgs.from_cli_args(args)
    engine = (llm_engine
              if llm_engine is not None else AsyncLLMEngine.from_engine_args(
        engine_args, usage_context=UsageContext.API_SERVER))

    return app


async def run_server(args: Namespace,
                     llm_engine: Optional[AsyncLLMEngine] = None,
                     **uvicorn_kwargs: Any) -> None:
    logger.info("vLLM API server version %s", VLLM_VERSION)
    logger.info("args: %s", args)

    set_ulimit()

    app = await init_app(args, llm_engine)
    assert engine is not None

    shutdown_task = await serve_http(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_keep_alive=TIMEOUT_KEEP_ALIVE,
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
        ssl_ca_certs=args.ssl_ca_certs,
        ssl_cert_reqs=args.ssl_cert_reqs,
        **uvicorn_kwargs,
    )

    await shutdown_task


if __name__ == "__main__":
    try:
        parser = FlexibleArgumentParser()
        parser.add_argument("--host", type=str, default=None)
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument("--ssl-keyfile", type=str, default=None)
        parser.add_argument("--ssl-certfile", type=str, default=None)
        parser.add_argument("--ssl-ca-certs",
                            type=str,
                            default=None,
                            help="The CA certificates file")
        parser.add_argument(
            "--ssl-cert-reqs",
            type=int,
            default=int(ssl.CERT_NONE),
            help="Whether client certificate is required (see stdlib ssl module's)"
        )
        parser.add_argument(
            "--root-path",
            type=str,
            default=None,
            help="FastAPI root_path when app is behind a path based routing proxy")
        parser.add_argument("--log-level", type=str, default="debug")
        parser = AsyncEngineArgs.add_cli_args(parser)
        args = parser.parse_args()

        asyncio.run(run_server(args))
    except Exception as e:
        logger.error(str(e))
        raise
