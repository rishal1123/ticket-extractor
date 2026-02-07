"""Quick test of Znuny client."""
from znuny_client import ZnunyClient

def test():
    client = ZnunyClient()

    # Test search for a known ticket ID pattern
    test_ids = ["OOR-2001287", "DHG-12345", "ROL250141"]

    for ticket_id in test_ids:
        print(f"\nSearching for: {ticket_id}")
        found, znuny_id = client.check_ticket_sync(ticket_id)
        print(f"  Found: {found}, Znuny ID: {znuny_id}")

    client.close()
    print("\nTest complete.")

if __name__ == "__main__":
    test()
