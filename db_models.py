from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class SalonDB(Base):
    __tablename__ = "salons"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    phone = Column(String)
    address = Column(String)