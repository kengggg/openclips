from openclips.ble_bleak import BleakTransport
from openclips.constants import INDICATE_UUID


class FakeBleakClient:
    def __init__(self):
        self.disconnected_cb = None
        self.connected = False
        self.notify_cb = None

    def set_disconnected_callback(self, cb):
        self.disconnected_cb = cb

    async def connect(self):
        self.connected = True

    async def start_notify(self, uuid, cb):
        assert str(uuid) == str(INDICATE_UUID)
        self.notify_cb = cb

    async def disconnect(self):
        self.connected = False
        if self.disconnected_cb:
            self.disconnected_cb(self)

    async def write_gatt_char(self, uuid, data, response=True):
        return None


def test_bleak_disconnect_callback_sets_alive_and_close_joins():
    client = FakeBleakClient()
    t = BleakTransport("AA:BB:CC:DD:EE:FF", client=client)
    assert t.alive
    client.disconnected_cb(client)
    assert not t.alive
    t.close()
    t.close()
    assert not t._thread.is_alive()


def test_bleak_failed_connect_closes_loop():
    class Boom(FakeBleakClient):
        async def connect(self):
            raise OSError("no adapter")

    try:
        BleakTransport("AA:BB:CC:DD:EE:FF", connect_timeout=1, client=Boom())
    except OSError:
        pass
    else:
        raise AssertionError("expected connect failure")
