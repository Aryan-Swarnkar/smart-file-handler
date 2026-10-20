
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import File as FileModel
from models import User as UserModel


# -------------------------
# User database operations
# -------------------------

def get_user_by_email(db: Session, email: str) -> UserModel | None:
    return db.scalar(
        select(UserModel).where(UserModel.email == email)
    )


def get_user_by_id(db: Session, user_id: int) -> UserModel | None:
    return db.get(UserModel, user_id)


def create_user(db: Session, user: UserModel) -> UserModel:
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    except Exception:
        db.rollback()
        raise


# -------------------------
# File database operations
# -------------------------

def get_file_by_id(
    db: Session,
    file_id: int,
    user_id: int,
) -> FileModel | None:
    return db.scalar(
        select(FileModel).where(
            FileModel.id == file_id,
            FileModel.user_id == user_id,
        )
    )


def get_files_by_user(
    db: Session,
    user_id: int,
) -> list[FileModel]:
    return list(
        db.scalars(
            select(FileModel)
            .where(FileModel.user_id == user_id)
            .order_by(FileModel.created_at.desc())
        ).all()
    )


def create_file(db: Session, file: FileModel) -> FileModel:
    try:
        db.add(file)
        db.commit()
        db.refresh(file)
        return file
    except Exception:
        db.rollback()
        raise


def save_file(db: Session, file: FileModel) -> FileModel:
    try:
        db.commit()
        db.refresh(file)
        return file
    except Exception:
        db.rollback()
        raise


def delete_file(db: Session, file: FileModel) -> None:
    try:
        db.delete(file)
        db.commit()
    except Exception:
        db.rollback()
        raise
