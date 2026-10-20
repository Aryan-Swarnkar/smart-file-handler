from __future__ import annotations

import mimetypes
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from settings import settings
from fastapi import (
    APIRouter,
    Depends,
    File as FastAPIFile,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse as DownloadResponse
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import File as FileModel
from models import User as UserModel
from schemas import (
    FileCategory,
    FileResponse as FileMetadataResponse,
    UserCreate,
    UserResponse,
    LoginRequest,
    TokenResponse,
    FileUpdate,
)


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
MAX_UPLOAD_SIZE = settings.MAX_UPLOAD_SIZE

password_hasher = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)

router = APIRouter()


def create_access_token(user_id: int) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": str(user_id),
        "exp": expires_at,
    }

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
    db: Session = Depends(get_db),
) -> UserModel:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        user_id = int(payload["sub"])

    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.scalar(
        select(UserModel).where(UserModel.id == user_id)
    )

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

def get_owned_file(
    file_id: int,
    user_id: int,
    db: Session,
) -> FileModel:
    file_record = db.scalar(
        select(FileModel).where(
            FileModel.id == file_id,
            FileModel.user_id == user_id,
        )
    )

    if file_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    return file_record


def get_stored_path(file_record: FileModel) -> Path:
    path = (BASE_DIR / file_record.file_path).resolve()

    # Prevent paths in the database from escaping the uploads directory.
    if not path.is_relative_to(UPLOAD_DIR.resolve()):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid stored file path",
        )

    return path


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

# --------------------------------------------------
# 1. Health route
# --------------------------------------------------

@router.get("/health")
def health_check():
    return {"status": "ok"}


# --------------------------------------------------
# 2. Home route
# --------------------------------------------------

@router.get("/")
def home():
    return {
        "message": "Welcome to Smart File Handler",
        "docs": "/docs",
    }


# --------------------------------------------------
# 3. Register route
# --------------------------------------------------

@router.post(
    "/auth/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_user(
    data: UserCreate,
    db: Session = Depends(get_db),
):
    email = str(data.email).lower()

    existing_user = db.scalar(
        select(UserModel).where(UserModel.email == email)
    )

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

    db.add(user)

    try:
        db.commit()
        db.refresh(user)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        )

    return user

# --------------------------------------------------
# 4. Login route
# --------------------------------------------------

@router.post("/auth/login", response_model=TokenResponse)
def login_user(
    data: LoginRequest,
    db: Session = Depends(get_db),
):
    email = str(data.email).lower()

    user = db.scalar(
        select(UserModel).where(UserModel.email == email)
    )

    if user is None or not password_hasher.verify(
        data.password, user.password_hash
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

    token = create_access_token(user.id)

    return TokenResponse(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

# --------------------------------------------------
# 5. Upload a file
# --------------------------------------------------

@router.post(
    "/files",
    response_model=FileMetadataResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_file(
    upload: UploadFile = FastAPIFile(...),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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

    # Determine a basic type from the file extension.
    mime_type = (
        mimetypes.guess_type(original_name)[0]
        or "application/octet-stream"
    )

    file_type = detect_file_type(mime_type)

    # Never use the client-supplied filename as the disk filename.
    stored_name = f"{uuid.uuid4().hex}{extension}"
    relative_path = (
        Path("uploads") / str(current_user.id) / stored_name
    )
    destination = BASE_DIR / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)

    total_size = 0

    try:
        # Copy in chunks so the entire file is not loaded into memory.
        with destination.open("wb") as output:
            while True:
                chunk = upload.file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Maximum upload size is 25 MB",
                    )

                output.write(chunk)

        file_record = FileModel(
            user_id=current_user.id,
            file_name=original_name,
            file_path=relative_path.as_posix(),
            file_type=file_type,
            file_extension=extension or None,
            mime_type=mime_type,
            category=None,
            processing_status="pending",
        )

        db.add(file_record)
        db.commit()
        db.refresh(file_record)

        return file_record

    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise

    finally:
        upload.file.close()


# --------------------------------------------------
# 6. List the logged-in user's files
# --------------------------------------------------

@router.get("/files", response_model=list[FileMetadataResponse])
def list_files(
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    files = db.scalars(
        select(FileModel)
        .where(FileModel.user_id == current_user.id)
        .order_by(FileModel.created_at.desc())
    ).all()

    return files

# --------------------------------------------------
# 7. Get one file's metadata
# --------------------------------------------------

@router.get(
    "/files/{file_id}",
    response_model=FileMetadataResponse,
)
def get_file(
    file_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_owned_file(file_id, current_user.id, db)

# --------------------------------------------------
# 8. Update file metadata
# --------------------------------------------------

@router.patch(
    "/files/{file_id}",
    response_model=FileMetadataResponse,
)
def update_file(
    file_id: int,
    data: FileUpdate,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    file_record = get_owned_file(file_id, current_user.id, db)
    changes = data.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one field to update",
        )

    if changes.get("file_name") is not None:
        file_record.file_name = changes["file_name"]

    if "category" in changes:
        category = changes["category"]
        file_record.category = (
            category.value if category is not None else None
        )

    db.commit()
    db.refresh(file_record)

    return file_record

# --------------------------------------------------
# 9. Delete a file
# --------------------------------------------------

@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_file(
    file_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    file_record = get_owned_file(file_id, current_user.id, db)
    disk_path = get_stored_path(file_record)

    db.delete(file_record)
    db.commit()

    try:
        disk_path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="File metadata was deleted, but the stored file could not be removed",
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)

# --------------------------------------------------
# 10. Download a file
# --------------------------------------------------

@router.get("/files/{file_id}/download")
def download_file(
    file_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DownloadResponse:
    file_record = get_owned_file(file_id, current_user.id, db)
    disk_path = get_stored_path(file_record)

    if not disk_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stored file not found",
        )

    return DownloadResponse(
        path=disk_path,
        filename=file_record.file_name,
        media_type=file_record.mime_type or "application/octet-stream",
    )