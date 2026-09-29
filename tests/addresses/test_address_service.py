from uuid import uuid4

import pytest

from app.modules.addresses.exceptions.address_exceptions import AddressNotFoundError
from app.modules.addresses.services.address_service import AddressService


@pytest.fixture
def address_service(fake_address_repository) -> AddressService:
    return AddressService(fake_address_repository)


async def _make_address(service, user_id, **overrides):
    defaults = dict(
        recipient_name="Jane Doe",
        line1="123 Main St",
        line2=None,
        city="Springfield",
        state="IL",
        postal_code="62701",
        country="US",
        is_default=False,
    )
    defaults.update(overrides)
    return await service.create_address(user_id=user_id, **defaults)


async def test_first_address_becomes_default_automatically(address_service):
    user_id = uuid4()

    address = await _make_address(address_service, user_id, is_default=False)

    assert address.is_default is True


async def test_second_address_is_not_default_unless_requested(address_service):
    user_id = uuid4()
    await _make_address(address_service, user_id)

    second = await _make_address(address_service, user_id, line1="456 Oak Ave")

    assert second.is_default is False


async def test_creating_a_new_default_unsets_the_old_one(address_service):
    user_id = uuid4()
    first = await _make_address(address_service, user_id)

    second = await _make_address(
        address_service, user_id, line1="456 Oak Ave", is_default=True
    )

    refreshed_first = await address_service.get_address(
        address_id=first.id, user_id=user_id
    )
    assert refreshed_first.is_default is False
    assert second.is_default is True


async def test_list_addresses_only_returns_the_caller_own(address_service):
    user_a = uuid4()
    user_b = uuid4()
    await _make_address(address_service, user_a)
    await _make_address(address_service, user_b)

    results = await address_service.list_addresses(user_a)

    assert len(results) == 1
    assert results[0].user_id == user_a


async def test_get_another_users_address_404s(address_service):
    owner = uuid4()
    other = uuid4()
    address = await _make_address(address_service, owner)

    with pytest.raises(AddressNotFoundError):
        await address_service.get_address(address_id=address.id, user_id=other)


async def test_update_another_users_address_404s(address_service):
    owner = uuid4()
    other = uuid4()
    address = await _make_address(address_service, owner)

    with pytest.raises(AddressNotFoundError):
        await address_service.update_address(
            address_id=address.id,
            user_id=other,
            recipient_name="Hacker",
            line1=None,
            line2=None,
            city=None,
            state=None,
            postal_code=None,
            country=None,
            is_default=None,
        )


async def test_update_sets_a_new_default(address_service):
    user_id = uuid4()
    first = await _make_address(address_service, user_id)
    second = await _make_address(address_service, user_id, line1="456 Oak Ave")

    updated = await address_service.update_address(
        address_id=second.id,
        user_id=user_id,
        recipient_name=None,
        line1=None,
        line2=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
        is_default=True,
    )

    refreshed_first = await address_service.get_address(
        address_id=first.id, user_id=user_id
    )
    assert updated.is_default is True
    assert refreshed_first.is_default is False


async def test_cannot_unset_the_only_default_address(address_service):
    user_id = uuid4()
    address = await _make_address(address_service, user_id)
    assert address.is_default is True

    updated = await address_service.update_address(
        address_id=address.id,
        user_id=user_id,
        recipient_name=None,
        line1=None,
        line2=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
        is_default=False,
    )

    # Refused -- stays the default, since there's no other address to
    # fall back to at checkout.
    assert updated.is_default is True


async def test_deleting_the_default_promotes_another_address(address_service):
    user_id = uuid4()
    first = await _make_address(address_service, user_id)
    second = await _make_address(address_service, user_id, line1="456 Oak Ave")
    assert first.is_default is True

    await address_service.delete_address(address_id=first.id, user_id=user_id)

    refreshed_second = await address_service.get_address(
        address_id=second.id, user_id=user_id
    )
    assert refreshed_second.is_default is True


async def test_delete_another_users_address_404s(address_service):
    owner = uuid4()
    other = uuid4()
    address = await _make_address(address_service, owner)

    with pytest.raises(AddressNotFoundError):
        await address_service.delete_address(address_id=address.id, user_id=other)