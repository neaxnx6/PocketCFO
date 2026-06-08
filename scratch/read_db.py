import sqlite3

def read_db():
    conn = sqlite3.connect("pocket_cfo.db")
    cursor = conn.cursor()
    
    print("USERS:")
    cursor.execute("SELECT * FROM users")
    for row in cursor.fetchall():
        print(row)
        
    print("\nENVELOPES:")
    cursor.execute("SELECT id, user_id, name, current_amount, target_amount, is_debt, is_goal, min_payment FROM envelopes")
    for row in cursor.fetchall():
        print(row)
        
    print("\nTRANSACTIONS:")
    cursor.execute("SELECT * FROM transactions")
    for row in cursor.fetchall():
        print(row)
        
    conn.close()

if __name__ == "__main__":
    read_db()
