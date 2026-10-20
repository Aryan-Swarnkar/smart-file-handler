
import mimetypes
import uuid
from pathlib import Path

import jwt
from fastapi import HTTPException, UploadFile, status
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import repositories
from models import File as FileModel
from models import User as UserModel
from schemas import FileCategory, UserCreate
from settings import BASE_DIR, settings

from datetime import datetime, timedelta, timezone

# -------------------------
# Configuration
# -------------------------

UPLOAD_DIR = BASE_DIR / "uploads"

password_hasher = PasswordHash.recommended()


# -------------------------
# Authentication services
# -------------------------

def register_user(db: Session, data: UserCreate) -> UserModel:
    email = str(data.email).lower()

    existing_user = repositories.get_user_by_email(db, email)

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        )

    user = UserModel(
        name=data.name,
        email=email,
        password_hash=password_hasher.hash(data.password),
    )

    try:
        return repositories.create_user(db, user)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        )


def login_user(
    db: Session,
    email: str,
    password: str,
) -> dict:
    user = repositories.get_user_by_email(
        db, email.lower()
    )

    if user is None or not password_hasher.verify(
        password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is inactive",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )

    token = jwt.encode(
        {
            "sub": str(user.id),
            "exp": expires_at,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_at,
    }


def get_authenticated_user(
    db: Session,
    token: str | None,
) -> UserModel:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        user_id = int(payload["sub"])
    except (
        jwt.InvalidTokenError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = repositories.get_user_by_id(db, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is inactive",
        )

    return user


# -------------------------
# File helper functions
# -------------------------

def get_owned_file(
    db: Session,
    file_id: int,
    user_id: int,
) -> FileModel:
    file = repositories.get_file_by_id(
        db, file_id, user_id
    )

    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    return file


def detect_file_type(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"

    if mime_type.startswith("video/"):
        return "video"

    if mime_type.startswith("audio/"):
        return "audio"

    if (
        mime_type.startswith("text/")
        or mime_type == "application/pdf"
        or "officedocument" in mime_type
        or "msword" in mime_type
        or "spreadsheet" in mime_type
        or "presentation" in mime_type
    ):
        return "document"

    return "other"


def get_stored_path(file: FileModel) -> Path:
    upload_root = UPLOAD_DIR.resolve()
    stored_path = (BASE_DIR / file.file_path).resolve()

    if not stored_path.is_relative_to(upload_root):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid stored file path",
        )

    return stored_path


# -------------------------
# File CRUD services
# -------------------------

def upload_file(
    db: Session,
    user_id: int,
    upload: UploadFile,
) -> FileModel:
    original_name = Path(
        (upload.filename or "").replace("\\", "/")
    ).name

    if not original_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A filename is required",
        )

    if len(original_name) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename must be 255 characters or fewer",
        )

    extension = Path(original_name).suffix.lower()

    if len(extension) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File extension is too long",
        )

    # This is a preliminary classification based on the extension.
    # It does not validate the actual contents of the uploaded file.
    mime_type = (
        mimetypes.guess_type(original_name)[0]
        or "application/octet-stream"
    )

    file_type = detect_file_type(mime_type)

    stored_name = f"{uuid.uuid4().hex}{extension}"

    destination = UPLOAD_DIR / str(user_id) / stored_name
    relative_path = destination.relative_to(BASE_DIR)

    destination.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0

    try:
        with destination.open("wb") as output:
            while True:
                chunk = upload.file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > settings.MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="File exceeds the upload size limit",
                    )

                output.write(chunk)

        file = FileModel(
            user_id=user_id,
            file_name=original_name,
            file_path=relative_path.as_posix(),
            file_type=file_type,
            file_extension=extension or None,
            mime_type=mime_type,
            category=None,
            processing_status="pending",
        )

        return repositories.create_file(db, file)

    except Exception:
        destination.unlink(missing_ok=True)
        raise

    finally:
        upload.file.close()


def list_user_files(
    db: Session,
    user_id: int,
) -> list[FileModel]:
    return repositories.get_files_by_user(db, user_id)


def get_user_file(
    db: Session,
    file_id: int,
    user_id: int,
) -> FileModel:
    return get_owned_file(db, file_id, user_id)


def update_user_file(
    db: Session,
    file_id: int,
    user_id: int,
    changes: dict,
) -> FileModel:
    file = get_owned_file(db, file_id, user_id)

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one field to update",
        )

    if "file_name" in changes:
        new_name = changes["file_name"]

        if new_name is None or not new_name.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Filename cannot be empty",
            )

        safe_name = Path(new_name.replace("\\", "/")).name

        if not safe_name or len(safe_name) > 255:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid filename",
            )

        file.file_name = safe_name

    if "category" in changes:
        category = changes["category"]

        if category is None:
            file.category = None
        elif isinstance(category, FileCategory):
            file.category = category.value
        else:
            file.category = FileCategory(category).value

    return repositories.save_file(db, file)


def delete_user_file(
    db: Session,
    file_id: int,
    user_id: int,
) -> None:
    file = get_owned_file(db, file_id, user_id)
    stored_path = get_stored_path(file)

    repositories.delete_file(db, file)

    try:
        stored_path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database record deleted, but stored file cleanup failed",
        ) from exc


def get_user_file_for_download(
    db: Session,
    file_id: int,
    user_id: int,
) -> tuple[FileModel, Path]:
    file = get_owned_file(db, file_id, user_id)
    stored_path = get_stored_path(file)

    if not stored_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stored file not found",
        )

    return file, stored_path
