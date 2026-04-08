from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class BlogCategory(Base):
    __tablename__ = "blog_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    articles: Mapped[list["BlogArticle"]] = relationship(
        back_populates="category",
        cascade="all, save-update",
    )


class BlogArticle(Base):
    __tablename__ = "blog_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    title: Mapped[str] = mapped_column(String(220), nullable=False)
    slug: Mapped[str] = mapped_column(String(240), unique=True, index=True, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)

    hero_image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    hero_image_alt: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hero_image_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    meta_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    category_id: Mapped[int | None] = mapped_column(ForeignKey("blog_categories.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    category: Mapped["BlogCategory | None"] = relationship(back_populates="articles")
