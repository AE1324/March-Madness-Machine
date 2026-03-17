import os
from sqlalchemy import create_engine
from db import Base

def main():
    db_url = os.getenv("DATABASE_URL", "sqlite:///brackets.db")
    engine = create_engine(db_url, future=True)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("Dropped and recreated all tables.")

if __name__ == "__main__":
    main()