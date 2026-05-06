from silver_strategy.database import Session, DailyState
from database.db import SessionLocal
from database.models import Trade

print("Clearing Silver Strategy states...")
db = Session()
db.query(DailyState).delete()
db.commit()

print("Clearing Open/Pending trades from Engine...")
db2 = SessionLocal()
db2.query(Trade).filter(Trade.status.in_(['OPEN', 'PENDING'])).delete()
db2.commit()

print("\n✅ All old strategy states and active trades cleared successfully!")
print("Please run ./deploy.sh to restart the application.")
