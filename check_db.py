from database import engine
from sqlalchemy import inspect

inspector = inspect(engine)

print("Tables:")
print(inspector.get_table_names())

print("\nServices Columns:")
for column in inspector.get_columns("services"):
    print(column["name"], column["type"])