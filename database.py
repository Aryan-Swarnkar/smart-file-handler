from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy import create_engine

DATABASE_URL="postgresql+psycopg://postgres:iamnotaryan@localhost:5432/smart_file_handler"

engine = create_engine(
    DATABASE_URL
)

SessionLocal = sessionmaker(
    autoflush=False, autocommit=False, bind=engine,
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    except:
        db.close()


class Base(DeclarativeBase):
    pass
