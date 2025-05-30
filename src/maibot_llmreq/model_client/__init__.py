import asyncio
from typing import Callable, Any

from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk, ChatCompletion

from .base_client import BaseClient, APIResponse
from .. import _logger as logger
from ..config.config import (
    ModelInfo,
    ModelUsageArgConfigItem,
    RequestConfig,
    ModuleConfig,
)
from ..exceptions import (
    NetworkConnectionError,
    ReqAbortException,
    RespNotOkException,
    RespParseException,
)
from ..payload_content.message import Message
from ..payload_content.resp_format import RespFormat
from ..payload_content.tool_option import ToolOption
from ..utils import compress_messages


def _check_retry(
    remain_try: int,
    retry_interval: int,
    can_retry_msg: str,
    cannot_retry_msg: str,
    can_retry_callable: Callable | None = None,
    **kwargs,
) -> tuple[int, Any | None]:
    """
    辅助函数：检查是否可以重试
    :param remain_try: 剩余尝试次数
    :param retry_interval: 重试间隔
    :param can_retry_msg: 可以重试时的提示信息
    :param cannot_retry_msg: 不可以重试时的提示信息
    :return: (等待间隔（如果为0则不等待，为-1则不再请求该模型）, 新的消息列表（适用于压缩消息）)
    """
    if remain_try > 0:
        # 还有重试机会
        logger.warning(f"{can_retry_msg}")
        if can_retry_callable is not None:
            return retry_interval, can_retry_callable(**kwargs)
        else:
            return retry_interval, None
    else:
        # 达到最大重试次数
        logger.warning(f"{cannot_retry_msg}")
        return -1, None  # 不再重试请求该模型


def _handle_resp_not_ok(
    e: RespNotOkException,
    task_name: str,
    model_name: str,
    remain_try: int,
    retry_interval: int = 10,
    messages: tuple[list[Message], bool] | None = None,
):
    """
    处理响应错误异常
    :param e: 异常对象
    :param task_name: 任务名称
    :param model_name: 模型名称
    :param remain_try: 剩余尝试次数
    :param retry_interval: 重试间隔
    :param messages: (消息列表, 是否已压缩过)
    :return: (等待间隔（如果为0则不等待，为-1则不再请求该模型）, 新的消息列表（适用于压缩消息）)
    """
    # 响应错误
    if e.status_code in [400, 401, 402, 403, 404]:
        # 客户端错误
        logger.warning(
            f"任务-'{task_name}' 模型-'{model_name}'\n"
            f"请求失败，错误代码-{e.status_code}，错误信息-{e.message}"
        )
        return -1, None  # 不再重试请求该模型
    elif e.status_code == 413:
        if messages and not messages[1]:
            # 消息列表不为空且未压缩，尝试压缩消息
            return _check_retry(
                remain_try,
                0,
                can_retry_msg=(
                    f"任务-'{task_name}' 模型-'{model_name}'\n"
                    "请求体过大，尝试压缩消息后重试"
                ),
                cannot_retry_msg=(
                    f"任务-'{task_name}' 模型-'{model_name}'\n"
                    "请求体过大，压缩消息后仍然过大，放弃请求"
                ),
                can_retry_callable=compress_messages,
                messages=messages[0],
            )
        # 没有消息可压缩
        logger.warning(
            f"任务-'{task_name}' 模型-'{model_name}'\n"
            "请求体过大，无法压缩消息，放弃请求。"
        )
        return -1, None
    elif e.status_code == 429:
        # 请求过于频繁
        return _check_retry(
            remain_try,
            retry_interval,
            can_retry_msg=(
                f"任务-'{task_name}' 模型-'{model_name}'\n"
                f"请求过于频繁，将于{retry_interval}秒后重试"
            ),
            cannot_retry_msg=(
                f"任务-'{task_name}' 模型-'{model_name}'\n"
                "请求过于频繁，超过最大重试次数，放弃请求"
            ),
        )
    elif e.status_code >= 500:
        # 服务器错误
        return _check_retry(
            remain_try,
            retry_interval,
            can_retry_msg=(
                f"任务-'{task_name}' 模型-'{model_name}'\n"
                f"服务器错误，将于{retry_interval}秒后重试"
            ),
            cannot_retry_msg=(
                f"任务-'{task_name}' 模型-'{model_name}'\n"
                "服务器错误，超过最大重试次数，请稍后再试"
            ),
        )
    else:
        # 未知错误
        logger.warning(
            f"任务-'{task_name}' 模型-'{model_name}'\n"
            f"未知错误，错误代码-{e.status_code}，错误信息-{e.message}"
        )
        return -1, None


def default_exception_handler(
    e: Exception,
    task_name: str,
    model_name: str,
    remain_try: int,
    retry_interval: int = 10,
    messages: tuple[list[Message], bool] | None = None,
) -> tuple[int, list[Message] | None]:
    """
    默认异常处理函数
    :param e: 异常对象
    :param task_name: 任务名称
    :param model_name: 模型名称
    :param remain_try: 剩余尝试次数
    :param retry_interval: 重试间隔
    :param messages: (消息列表, 是否已压缩过)
    :return (等待间隔（如果为0则不等待，为-1则不再请求该模型）, 新的消息列表（适用于压缩消息）)
    """

    if isinstance(e, NetworkConnectionError):  # 网络连接错误
        return _check_retry(
            remain_try,
            retry_interval,
            can_retry_msg=(
                f"任务-'{task_name}' 模型-'{model_name}'\n"
                f"连接异常，将于{retry_interval}秒后重试"
            ),
            cannot_retry_msg=(
                f"任务-'{task_name}' 模型-'{model_name}'\n"
                f"连接异常，超过最大重试次数，请检查网络连接状态或URL是否正确"
            ),
        )
    elif isinstance(e, ReqAbortException):
        logger.warning(
            f"任务-'{task_name}' 模型-'{model_name}'\n请求被中断，详细信息-{str(e.message)}"
        )
        return -1, None  # 不再重试请求该模型
    elif isinstance(e, RespNotOkException):
        return _handle_resp_not_ok(
            e,
            task_name,
            model_name,
            remain_try,
            retry_interval,
            messages,
        )
    elif isinstance(e, RespParseException):
        # 响应解析错误
        logger.error(
            f"任务-'{task_name}' 模型-'{model_name}'\n"
            f"响应解析错误，错误信息-{e.message}\n"
        )
        logger.debug(f"附加内容:\n{str(e.ext_info)}")
        return -1, None  # 不再重试请求该模型
    else:
        logger.error(
            f"任务-'{task_name}' 模型-'{model_name}'\n未知异常，错误信息-{str(e)}"
        )
        return -1, None  # 不再重试请求该模型


class ModelRequestHandler:
    """
    模型请求处理器
    """

    def __init__(
        self,
        task_name: str,
        config: ModuleConfig,
        api_client_map: dict[str, BaseClient],
    ):
        self.task_name: str = task_name
        """任务名称"""

        self.client_map: dict[str, BaseClient] = {}
        """API客户端列表"""

        self.configs: list[tuple[ModelInfo, ModelUsageArgConfigItem]] = []
        """模型参数配置"""

        self.req_conf: RequestConfig = config.req_conf
        """请求配置"""

        # 获取模型与使用配置
        for model_usage in config.task_model_arg_map[task_name].usage:
            if model_usage.name not in config.models:
                logger.error(f"Model '{model_usage.name}' not found in ModelManager")
                raise KeyError(f"Model '{model_usage.name}' not found in ModelManager")
            model_info = config.models[model_usage.name]

            if model_info.api_provider not in self.client_map:
                # 缓存API客户端
                self.client_map[model_info.api_provider] = api_client_map[
                    model_info.api_provider
                ]

            self.configs.append((model_info, model_usage))  # 添加模型与使用配置

    async def get_response(
        self,
        messages: list[Message],
        tool_options: list[ToolOption] | None = None,
        response_format: RespFormat | None = None,  # 暂不启用
        stream_response_handler: Callable[
            [AsyncStream[ChatCompletionChunk], asyncio.Event | None], APIResponse
        ]
        | None = None,
        async_response_parser: Callable[[ChatCompletion], APIResponse] | None = None,
        interrupt_flag: asyncio.Event | None = None,
    ) -> APIResponse:
        """
        获取对话响应
        :param messages: 消息列表
        :param tool_options: 工具选项列表
        :param response_format: 响应格式
        :param stream_response_handler: 流式响应处理函数（可选）
        :param async_response_parser: 响应解析函数（可选）
        :param interrupt_flag: 中断信号量（可选，默认为None）
        :return: APIResponse
        """
        # 遍历可用模型，若获取响应失败，则使用下一个模型继续请求
        for config_item in self.configs:
            client = self.client_map[config_item[0].api_provider]
            model_info: ModelInfo = config_item[0]
            model_usage_config: ModelUsageArgConfigItem = config_item[1]

            remain_try = (
                model_usage_config.max_retry or self.req_conf.max_retry
            ) + 1  # 初始化：剩余尝试次数 = 最大重试次数 + 1

            compressed_messages = None
            retry_interval = self.req_conf.retry_interval
            while remain_try > 0:
                try:
                    return await client.get_response(
                        model_info,
                        message_list=(compressed_messages or messages),
                        tool_options=tool_options,
                        max_tokens=model_usage_config.max_tokens
                        or self.req_conf.default_max_tokens,
                        temperature=model_usage_config.temperature
                        or self.req_conf.default_temperature,
                        response_format=response_format,
                        stream_response_handler=stream_response_handler,
                        async_response_parser=async_response_parser,
                        interrupt_flag=interrupt_flag,
                    )
                except Exception as e:
                    logger.trace(e)
                    remain_try -= 1  # 剩余尝试次数减1

                    # 处理异常
                    handle_res = default_exception_handler(
                        e,
                        self.task_name,
                        model_info.name,
                        remain_try,
                        retry_interval=self.req_conf.retry_interval,
                        messages=(messages, compressed_messages is not None),
                    )

                    if handle_res[0] == -1:
                        # 等待间隔为-1，表示不再请求该模型
                        remain_try = 0
                    elif handle_res[0] != 0:
                        # 等待间隔不为0，表示需要等待
                        await asyncio.sleep(handle_res[0])
                        retry_interval *= 2

                    if handle_res[1] is not None:
                        # 压缩消息
                        compressed_messages = handle_res[1]

        logger.error(f"任务-'{self.task_name}' 请求执行失败，所有模型均不可用")
        raise RuntimeError("请求失败，所有模型均不可用")  # 所有请求尝试均失败

    async def get_embedding(
        self,
        embedding_input: str,
    ) -> APIResponse:
        """
        获取嵌入向量
        :param embedding_input: 嵌入输入
        :return: APIResponse
        """
        for config in self.configs:
            client = self.client_map[config[0].api_provider]
            model_info: ModelInfo = config[0]
            model_usage_config: ModelUsageArgConfigItem = config[1]
            remain_try = (
                model_usage_config.max_retry or self.req_conf.max_retry
            ) + 1  # 初始化：剩余尝试次数 = 最大重试次数 + 1

            while remain_try:
                try:
                    return await client.get_embedding(
                        model_info=model_info,
                        embedding_input=embedding_input,
                    )
                except Exception as e:
                    logger.trace(e)
                    remain_try -= 1  # 剩余尝试次数减1

                    # 处理异常
                    handle_res = default_exception_handler(
                        e,
                        self.task_name,
                        model_info.name,
                        remain_try,
                        retry_interval=self.req_conf.retry_interval,
                    )

                    if handle_res[0] == -1:
                        # 等待间隔为-1，表示不再请求该模型
                        remain_try = 0
                    elif handle_res[0] != 0:
                        # 等待间隔不为0，表示需要等待
                        await asyncio.sleep(handle_res[0])

        logger.error(f"任务-'{self.task_name}' 请求执行失败，所有模型均不可用")
        raise RuntimeError("请求失败，所有模型均不可用")  # 所有请求尝试均失败
