import base64
import io

from PIL import Image

from . import _logger as logger
from .payload_content.message import Message, MessageBuilder


def compress_messages(
    messages: list[Message], img_target_size: int = 1 * 1024 * 1024
) -> list[Message]:
    """
    压缩消息列表中的图片
    :param messages: 消息列表
    :param img_target_size: 图片目标大小，默认1MB
    :return: 压缩后的消息列表
    """

    def reformat_static_image(image_data: bytes) -> bytes:
        """
        将静态图片转换为JPEG格式
        :param image_data: 图片数据
        :return: 转换后的图片数据
        """
        try:
            image = Image.open(image_data)

            if image.format and (
                image.format.upper() in ["JPEG", "JPG", "PNG", "WEBP"]
            ):
                # 静态图像，转换为JPEG格式
                reformated_image_data = io.BytesIO()
                image.save(
                    reformated_image_data, format="JPEG", quality=95, optimize=True
                )
                image_data = reformated_image_data.getvalue()

            return image_data
        except Exception as e:
            logger.error(f"图片转换格式失败: {str(e)}")
            return image_data

    def rescale_image(
        image_data: bytes, scale: float
    ) -> tuple[bytes, tuple[int, int] | None, tuple[int, int] | None]:
        """
        缩放图片
        :param image_data: 图片数据
        :param scale: 缩放比例
        :return: 缩放后的图片数据
        """
        try:
            image = Image.open(image_data)

            # 原始尺寸
            original_size = (image.width, image.height)

            # 计算新的尺寸
            new_size = (int(original_size[0] * scale), int(original_size[1] * scale))

            output_buffer = io.BytesIO()

            if getattr(image, "is_animated", False):
                # 动态图片，处理所有帧
                frames = []
                new_size = (new_size[0] // 2, new_size[1] // 2)  # 动图，缩放尺寸再打折
                for frame_idx in range(getattr(image, "n_frames", 1)):
                    image.seek(frame_idx)
                    new_frame = image.copy()
                    new_frame = new_frame.resize(new_size, Image.Resampling.LANCZOS)
                    frames.append(new_frame)

                # 保存到缓冲区
                frames[0].save(
                    output_buffer,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    optimize=True,
                    duration=image.info.get("duration", 100),
                    loop=image.info.get("loop", 0),
                )
            else:
                # 静态图片，直接缩放保存
                resized_image = image.resize(new_size, Image.Resampling.LANCZOS)
                resized_image.save(
                    output_buffer, format="JPEG", quality=95, optimize=True
                )

            return output_buffer.getvalue(), original_size, new_size

        except Exception as e:
            logger.error(f"图片缩放失败: {str(e)}")
            import traceback

            logger.error(traceback.format_exc())
            return image_data, None, None

    def compress_base64_image(
        base64_data: str, target_size: int = 1 * 1024 * 1024
    ) -> str:
        original_b64_data_size = len(base64_data)  # 计算原始数据大小

        image_data = base64.b64decode(base64_data)

        # 先尝试转换格式为JPEG
        image_data = reformat_static_image(image_data)
        base64_data = base64.b64encode(image_data).decode("utf-8")
        if len(base64_data) <= target_size:
            # 如果转换后小于目标大小，直接返回
            logger.info(
                f"成功将图片转为JPEG格式，编码后大小: {len(base64_data) / 1024:.1f}KB"
            )
            return base64_data

        # 如果转换后仍然大于目标大小，进行尺寸压缩
        scale = min(1.0, target_size / len(base64_data))
        image_data, original_size, new_size = rescale_image(image_data, scale)
        base64_data = base64.b64encode(image_data).decode("utf-8")

        if original_size and new_size:
            logger.info(
                f"压缩图片: {original_size[0]}x{original_size[1]} -> {new_size[0]}x{new_size[1]}\n"
                f"压缩前大小: {original_b64_data_size / 1024:.1f}KB, 压缩后大小: {len(base64_data) / 1024:.1f}KB"
            )

        return base64_data

    compressed_messages = []
    for message in messages:
        if isinstance(message.content, list):
            # 检查content，如有图片则压缩
            message_builder = MessageBuilder()
            for content_item in message.content:
                if isinstance(content_item, tuple):
                    # 图片，进行压缩
                    message_builder.add_image_content(
                        content_item[0],
                        compress_base64_image(
                            content_item[1], target_size=img_target_size
                        ),
                    )
                else:
                    message_builder.add_text_content(content_item)
            compressed_messages.append(message_builder.build())
        else:
            compressed_messages.append(message)

    return compressed_messages
