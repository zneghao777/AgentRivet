"""RabbitMQ 接入：统一信封、连接拓扑、发布与消费辅助。"""

from .connection import mq_connection

__all__ = ["mq_connection"]
