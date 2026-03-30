from sqlalchemy import Column, Integer, String, ForeignKey
from database import Base

class Todolist(Base):
    __tablename__ = "todolist"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer)
    todo = Column(String)
    dead_date = Column(String)
    dead_time = Column(String)
    importance = Column(Integer)
    
class Availabletime(Base):
    __tablename__ = "availabletime"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer)
    date = Column(String)
    free_time_start = Column(String)
    free_time_end = Column(String)
    
class Communication(Base):
    __tablename__ = "communication"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String)
    usermessage = Column(String)