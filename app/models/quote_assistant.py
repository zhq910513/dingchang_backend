# encoding: utf-8
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.types import JSON

from app.core.db import Base


class QuoteCase(Base):
    """Quote assistant case.

    This table is intentionally independent from existing order tables. A case
    may point to an existing order, or stay as a new-order draft until the user
    explicitly converts it later.
    """

    __tablename__ = "quote_case_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    case_no = Column(String(64), nullable=False, comment="Business case number")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    session_id = Column(String(64), nullable=True, comment="AI assistant session id")
    order_id = Column(Integer, ForeignKey("order_new.id", ondelete="SET NULL"), nullable=True, comment="Existing order id")

    source_type = Column(String(32), nullable=False, server_default=text("'new_order_draft'"), comment="existing_order/new_order_draft")
    platform_code = Column(String(32), nullable=True, comment="Target platform code")
    platform_name = Column(String(64), nullable=True, comment="Target platform display name")
    status = Column(String(32), nullable=False, server_default=text("'collecting'"), comment="collecting/ready/waiting_sms/quoted/failed")

    quote_count = Column(Integer, nullable=False, server_default=text("0"), comment="Successful quote count")
    current_task_id = Column(Integer, nullable=True, comment="Current quote task id")

    draft_order_data = Column(JSON, nullable=False, comment="Draft order data collected by chat/OCR")
    normalized_data = Column(JSON, nullable=False, comment="Normalized quote-ready data")
    missing_requirements = Column(JSON, nullable=False, comment="Latest missing requirement list")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("case_no", name="uq_quote_case_case_no"),
        Index("ix_quote_case_owner_status", "owner_user_id", "status", "id"),
        Index("ix_quote_case_session", "session_id", "id"),
        Index("ix_quote_case_order", "order_id", "id"),
        Index("ix_quote_case_platform", "platform_code", "status", "id"),
        Index("ix_quote_case_updated", "updated_at", "id"),
    )


class QuoteCaseImage(Base):
    """Image candidate pool for quote assistant slot placement."""

    __tablename__ = "quote_case_image_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    quote_case_id = Column(Integer, ForeignKey("quote_case_new.id", ondelete="CASCADE"), nullable=False, comment="Quote case id")
    image_file_id = Column(Integer, ForeignKey("image_file_new.id", ondelete="SET NULL"), nullable=True, comment="Image file id")

    provided_slot_key = Column(String(64), nullable=True, comment="Slot chosen by uploader/user")
    predicted_slot_key = Column(String(64), nullable=True, comment="Classifier predicted slot")
    confirmed_slot_key = Column(String(64), nullable=False, comment="Backend confirmed slot for quote")
    confidence = Column(Numeric(6, 4), nullable=False, server_default=text("'0.0000'"), comment="Classifier confidence")
    method = Column(String(32), nullable=False, server_default=text("'rule'"), comment="classifier method")
    reason = Column(String(512), nullable=True, comment="Classifier reason")

    status = Column(String(32), nullable=False, server_default=text("'active'"), comment="candidate/active/inactive/replaced/deleted_by_user")
    storage_key = Column(String(512), nullable=False, comment="Object storage key")
    image_url = Column(String(512), nullable=False, server_default=text("''"), comment="Display url if available")
    original_name = Column(String(255), nullable=True, comment="Original file name")
    content_type = Column(String(128), nullable=True, comment="MIME type")
    md5 = Column(String(32), nullable=True, comment="MD5 hex")
    size = Column(BigInteger, nullable=False, server_default=text("0"), comment="File size")

    ocr_text_sample = Column(Text, nullable=True, comment="Text used for classification")
    text_features = Column(JSON, nullable=False, comment="Classifier features")
    created_by = Column(Integer, ForeignKey("user_new.id"), nullable=True, comment="Uploader user id")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("quote_case_id", "storage_key", name="uq_quote_case_image_case_storage"),
        Index("ix_quote_case_image_case_status", "quote_case_id", "status", "id"),
        Index("ix_quote_case_image_case_slot_status", "quote_case_id", "confirmed_slot_key", "status", "id"),
        Index("ix_quote_case_image_storage", "storage_key"),
        Index("ix_quote_case_image_file", "image_file_id"),
        Index("ix_quote_case_image_created_by", "created_by", "id"),
    )


class QuoteTask(Base):
    """A single platform quote attempt."""

    __tablename__ = "quote_task_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    quote_case_id = Column(Integer, ForeignKey("quote_case_new.id", ondelete="CASCADE"), nullable=False, comment="Quote case id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    platform_name = Column(String(64), nullable=True, comment="Platform display name")

    status = Column(String(32), nullable=False, server_default=text("'pending'"), comment="pending/waiting_sms/running/success/failed/cancelled")
    login_state = Column(String(32), nullable=False, server_default=text("'none'"), comment="none/sms_required/authenticated/failed")
    sms_phone_mask = Column(String(32), nullable=True, comment="Masked login phone")
    trace_id = Column(String(64), nullable=True, comment="Trace id")

    request_payload = Column(JSON, nullable=False, comment="Platform request payload")
    response_payload = Column(JSON, nullable=False, comment="Platform raw/fake response")
    result_payload = Column(JSON, nullable=False, comment="Normalized quote result")
    submitted_snapshot = Column(JSON, nullable=False, comment="Immutable snapshot at submit time")
    error_detail = Column(String(2048), nullable=True, comment="Error detail")

    started_at = Column(DateTime(timezone=False), nullable=True, comment="Started at")
    finished_at = Column(DateTime(timezone=False), nullable=True, comment="Finished at")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        Index("ix_quote_task_case_status", "quote_case_id", "status", "id"),
        Index("ix_quote_task_platform_status", "platform_code", "status", "id"),
        Index("ix_quote_task_trace", "trace_id"),
        Index("ix_quote_task_created", "created_at", "id"),
    )


class QuotePlatformAccount(Base):
    """Saved per-user platform login material for quote assistant."""

    __tablename__ = "quote_platform_account_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    platform_name = Column(String(64), nullable=True, comment="Platform display name")

    login_phone = Column(String(32), nullable=True, comment="Login/SMS phone")
    login_phone_mask = Column(String(32), nullable=True, comment="Masked login phone")
    account_username = Column(String(128), nullable=True, comment="Platform account username")
    password_ciphertext = Column(Text, nullable=True, comment="Encrypted platform password")
    secret_payload_ciphertext = Column(Text, nullable=True, comment="Encrypted future token/cookie payload")
    credential_payload = Column(JSON, nullable=False, comment="Non-secret login metadata")

    last_login_state = Column(String(32), nullable=False, server_default=text("'none'"), comment="none/sms_required/authenticated/failed")
    last_sms_at = Column(DateTime(timezone=False), nullable=True, comment="Last SMS trigger time")
    last_used_at = Column(DateTime(timezone=False), nullable=True, comment="Last quote usage time")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "platform_code", name="uq_quote_platform_account_owner_platform"),
        Index("ix_quote_platform_account_owner", "owner_user_id", "id"),
        Index("ix_quote_platform_account_platform", "platform_code", "id"),
        Index("ix_quote_platform_account_used", "last_used_at", "id"),
    )


class QuoteCaseEvent(Base):
    """Durable audit/memory events for quote assistant cases."""

    __tablename__ = "quote_case_event_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    quote_case_id = Column(Integer, ForeignKey("quote_case_new.id", ondelete="CASCADE"), nullable=False, comment="Quote case id")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    session_id = Column(String(64), nullable=True, comment="AI assistant session id")

    event_type = Column(String(32), nullable=False, comment="chat/input/image/task/status")
    role = Column(String(32), nullable=True, comment="user/assistant/system")
    content = Column(Text, nullable=True, comment="Text content")
    payload = Column(JSON, nullable=False, comment="Event payload")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")

    __table_args__ = (
        Index("ix_quote_case_event_case_id", "quote_case_id", "id"),
        Index("ix_quote_case_event_owner_session", "owner_user_id", "session_id", "id"),
        Index("ix_quote_case_event_type", "event_type", "id"),
    )
