# encoding: utf-8
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class QuotePlatformAccountType(Base):
    """Custom account type per user/platform, for example new-car or used-car."""

    __tablename__ = "quote_platform_account_type_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    platform_name = Column(String(64), nullable=True, comment="Platform display name")
    type_name = Column(String(64), nullable=False, comment="Custom account type name")
    description = Column(String(255), nullable=True, comment="Description")
    match_rules_json = Column(JSON, nullable=False, comment="Future auto-match rules")
    is_default = Column(Boolean, nullable=False, server_default=text("0"), comment="Default type flag")
    enabled = Column(Boolean, nullable=False, server_default=text("1"), comment="Enabled flag")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "platform_code", "type_name", name="uq_quote_platform_account_type_owner_platform_name"),
        Index("ix_quote_platform_account_type_owner_platform", "owner_user_id", "platform_code", "enabled", "id"),
        Index("ix_quote_platform_account_type_default", "owner_user_id", "platform_code", "is_default", "id"),
    )


class QuotePlatformAccountProfile(Base):
    """A single usable platform account profile with isolated runtime state."""

    __tablename__ = "quote_platform_account_profile_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    platform_name = Column(String(64), nullable=True, comment="Platform display name")

    account_type_id = Column(Integer, ForeignKey("quote_platform_account_type_new.id", ondelete="SET NULL"), nullable=True, comment="Account type id")
    account_type_name = Column(String(64), nullable=True, comment="Account type name snapshot")
    account_username = Column(String(128), nullable=False, comment="Platform account username")
    password_ciphertext = Column(Text, nullable=False, comment="Encrypted platform password")
    login_phone = Column(String(32), nullable=True, comment="Login/SMS phone")
    login_phone_mask = Column(String(32), nullable=True, comment="Masked login phone")
    email = Column(String(128), nullable=True, comment="Account email")
    account_owner_user_id = Column(Integer, ForeignKey("user_new.id", ondelete="SET NULL"), nullable=True, comment="Optional account owner user id")
    account_owner_name = Column(String(64), nullable=True, comment="Optional account owner name")

    auto_login = Column(Boolean, nullable=False, server_default=text("1"), comment="Allow auto login during quote")
    enabled = Column(Boolean, nullable=False, server_default=text("1"), comment="Enabled flag")
    login_status = Column(String(32), nullable=False, server_default=text("'not_logged_in'"), comment="Login status")
    quota_status = Column(String(32), nullable=False, server_default=text("'unknown'"), comment="unknown/available/warning/full/reset")
    quota_reset_at = Column(DateTime(timezone=False), nullable=True, comment="Quota reset time")
    browser_env_key = Column(String(128), nullable=False, comment="Browser profile isolation key")

    credential_payload = Column(JSON, nullable=False, comment="Non-secret credential metadata")
    secret_payload_ciphertext = Column(Text, nullable=True, comment="Encrypted future token/cookie payload")
    last_login_at = Column(DateTime(timezone=False), nullable=True, comment="Last login at")
    last_check_at = Column(DateTime(timezone=False), nullable=True, comment="Last account check at")
    last_used_at = Column(DateTime(timezone=False), nullable=True, comment="Last quote usage at")
    last_error = Column(String(2048), nullable=True, comment="Last login/quote error")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "platform_code", "account_username", "account_type_name", name="uq_quote_platform_account_profile_owner_platform_user_type"),
        Index("ix_quote_platform_account_profile_owner", "owner_user_id", "id"),
        Index("ix_quote_platform_account_profile_platform", "platform_code", "enabled", "id"),
        Index("ix_quote_platform_account_profile_type", "owner_user_id", "platform_code", "account_type_name", "enabled", "id"),
        Index("ix_quote_platform_account_profile_status", "owner_user_id", "login_status", "quota_status", "id"),
        Index("ix_quote_platform_account_profile_used", "last_used_at", "id"),
    )


class QuotePlatformAccountQuota(Base):
    """Business-side query quota for one platform account."""

    __tablename__ = "quote_platform_account_quota_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    account_id = Column(Integer, ForeignKey("quote_platform_account_profile_new.id", ondelete="CASCADE"), nullable=False, comment="Account profile id")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    period_type = Column(String(16), nullable=False, server_default=text("'day'"), comment="day/week/month")
    quota_limit = Column(Integer, nullable=False, server_default=text("0"), comment="Quota limit per period")
    used_count = Column(Integer, nullable=False, server_default=text("0"), comment="Used count in current period")
    period_start_at = Column(DateTime(timezone=False), nullable=False, comment="Current period start")
    period_end_at = Column(DateTime(timezone=False), nullable=False, comment="Current period end")
    last_consumed_at = Column(DateTime(timezone=False), nullable=True, comment="Last successful quote time")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_quote_platform_account_quota_account"),
        Index("ix_quote_platform_account_quota_owner_platform", "owner_user_id", "platform_code", "period_type", "id"),
        Index("ix_quote_platform_account_quota_period", "period_end_at", "id"),
    )


class QuotePlatformAccountSessionState(Base):
    """Safe queryable session summary for an isolated platform account runtime."""

    __tablename__ = "quote_platform_account_session_state_new"

    account_id = Column(Integer, ForeignKey("quote_platform_account_profile_new.id", ondelete="CASCADE"), primary_key=True, comment="Account profile id")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    status = Column(String(32), nullable=False, server_default=text("'offline'"), comment="offline/logging_in/waiting_challenge/authenticated/expired/disabled/login_failed")
    session_version = Column(Integer, nullable=False, server_default=text("0"), comment="CAS session version")
    session_generation = Column(String(64), nullable=False, server_default=text("''"), comment="Login generation id")

    jwt_issued_at = Column(BigInteger, nullable=True, comment="JWT issued timestamp")
    jwt_expires_at = Column(BigInteger, nullable=True, comment="JWT expires timestamp")
    last_login_at = Column(DateTime(timezone=False), nullable=True, comment="Last login flow time")
    last_authenticated_at = Column(DateTime(timezone=False), nullable=True, comment="Last authenticated time")
    last_keepalive_at = Column(DateTime(timezone=False), nullable=True, comment="Last keepalive time")
    last_business_at = Column(DateTime(timezone=False), nullable=True, comment="Last business call time")
    last_refresh_at = Column(DateTime(timezone=False), nullable=True, comment="Last token refresh time")
    last_validation_at = Column(DateTime(timezone=False), nullable=True, comment="Last validation time")
    last_error_code = Column(String(128), nullable=True, comment="Last error code")
    last_error_message = Column(String(2048), nullable=True, comment="Last error message")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        Index("ix_quote_platform_account_session_owner_status", "owner_user_id", "status", "account_id"),
        Index("ix_quote_platform_account_session_platform_status", "platform_code", "status", "account_id"),
        Index("ix_quote_platform_account_session_updated", "updated_at", "account_id"),
    )


class QuotePlatformAccountLoginTask(Base):
    """A login attempt/challenge lifecycle for a platform account profile."""

    __tablename__ = "quote_platform_account_login_task_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    account_id = Column(Integer, ForeignKey("quote_platform_account_profile_new.id", ondelete="CASCADE"), nullable=False, comment="Account profile id")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    platform_name = Column(String(64), nullable=True, comment="Platform display name")
    status = Column(String(32), nullable=False, server_default=text("'pending'"), comment="pending/running/needs_code/success/failed/expired")
    challenge_type = Column(String(32), nullable=True, comment="sms/security_code/captcha")
    challenge_prompt = Column(String(512), nullable=True, comment="Prompt for operator")
    challenge_payload = Column(JSON, nullable=False, comment="Safe challenge metadata")
    trace_id = Column(String(64), nullable=False, comment="Trace id")
    error_detail = Column(String(2048), nullable=True, comment="Error detail")
    started_at = Column(DateTime(timezone=False), nullable=True, comment="Started at")
    finished_at = Column(DateTime(timezone=False), nullable=True, comment="Finished at")
    expires_at = Column(DateTime(timezone=False), nullable=True, comment="Challenge expires at")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        Index("ix_quote_platform_account_login_task_account", "account_id", "id"),
        Index("ix_quote_platform_account_login_task_owner_status", "owner_user_id", "status", "id"),
        Index("ix_quote_platform_account_login_task_trace", "trace_id"),
        Index("ix_quote_platform_account_login_task_expires", "expires_at", "id"),
    )


class QuotePlatformAccountEvent(Base):
    """Audit trail for account profile changes and runtime events."""

    __tablename__ = "quote_platform_account_event_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    account_id = Column(Integer, ForeignKey("quote_platform_account_profile_new.id", ondelete="CASCADE"), nullable=False, comment="Account profile id")
    event_type = Column(String(32), nullable=False, comment="create/update/login/quota/status")
    operator_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=True, comment="Operator user id")
    before_json = Column(JSON, nullable=False, comment="Safe before snapshot")
    after_json = Column(JSON, nullable=False, comment="Safe after snapshot")
    message = Column(String(1024), nullable=True, comment="Human-readable event message")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")

    __table_args__ = (
        Index("ix_quote_platform_account_event_account", "account_id", "id"),
        Index("ix_quote_platform_account_event_type", "event_type", "id"),
        Index("ix_quote_platform_account_event_operator", "operator_user_id", "id"),
    )


class QuotePlatformDefaultConfig(Base):
    """Global quote default parameters per platform/account type."""

    __tablename__ = "quote_platform_default_config_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    platform_code = Column(String(32), nullable=False, comment="Platform code")
    platform_name = Column(String(64), nullable=True, comment="Platform display name")
    account_type_name = Column(String(64), nullable=False, server_default=text("''"), comment="Account type name, empty means common")
    default_values_json = Column(JSON, nullable=False, comment="Default quote request values keyed by platform form field name")
    enabled = Column(Boolean, nullable=False, server_default=text("1"), comment="Enabled flag")
    created_by = Column(Integer, ForeignKey("user_new.id", ondelete="SET NULL"), nullable=True, comment="Creator user id")
    updated_by = Column(Integer, ForeignKey("user_new.id", ondelete="SET NULL"), nullable=True, comment="Updater user id")
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("platform_code", "account_type_name", name="uq_quote_platform_default_config_platform_type"),
        Index("ix_quote_platform_default_config_platform", "platform_code", "enabled", "id"),
        Index("ix_quote_platform_default_config_updated", "updated_at", "id"),
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


class QuoteAssistantSession(Base):
    """DB-backed chat session for quote assistant.

    This replaces the local JSON conversation file in production while staying
    independent from existing order tables.
    """

    __tablename__ = "quote_assistant_session_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    session_id = Column(String(64), nullable=False, comment="Public session id")
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")

    title = Column(String(128), nullable=False, server_default=text("'新会话'"), comment="Session title")
    deleted = Column(Boolean, nullable=False, server_default=text("0"), comment="Soft deleted")
    message_count = Column(Integer, nullable=False, server_default=text("0"), comment="Visible message count")
    last_message_preview = Column(String(256), nullable=False, server_default=text("''"), comment="Last message preview")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_quote_assistant_session_id"),
        Index("ix_quote_assistant_session_owner_deleted_updated", "owner_user_id", "deleted", "updated_at", "id"),
    )


class QuoteAssistantMessage(Base):
    """DB-backed timeline message for quote assistant sessions."""

    __tablename__ = "quote_assistant_message_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    message_id = Column(String(64), nullable=False, comment="Public message id")
    session_id = Column(
        String(64),
        ForeignKey("quote_assistant_session_new.session_id", ondelete="CASCADE"),
        nullable=False,
        comment="Public session id",
    )
    owner_user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="Owner user id")

    role = Column(String(32), nullable=False, comment="user/assistant/system")
    content = Column(Text, nullable=False, comment="Message content")
    metadata_json = Column(JSON, nullable=False, comment="Safe display metadata")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment="Created at")
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="Updated at",
    )

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_quote_assistant_message_id"),
        Index("ix_quote_assistant_message_session_id", "session_id", "id"),
        Index("ix_quote_assistant_message_owner_session_id", "owner_user_id", "session_id", "id"),
        Index("ix_quote_assistant_message_created", "created_at", "id"),
    )
