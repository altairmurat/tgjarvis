from sqlalchemy import Column, Integer, Float, String, ForeignKey
from database import Base

class Contact(Base):
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    display_name = Column(String)
    email = Column(String)
    source = Column(String)
    confidence = Column(Float, default=1.0)
    
class Communication(Base):
    __tablename__ = "communication"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String)
    usermessage = Column(String)