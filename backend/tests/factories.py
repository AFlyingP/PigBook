from typing import Any

import factory


class ProbeRecord:
    def __init__(self, name: str, value: str, id: int | None = None) -> None:
        self.id = id
        self.name = name
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "value": self.value}
        if self.id is not None:
            data["id"] = self.id
        return data


class ProbeRecordFactory(factory.Factory):  # type: ignore[misc]
    class Meta:
        model = ProbeRecord

    id = factory.Sequence(lambda n: n + 1)
    name = factory.Sequence(lambda n: f"probe_entity_{n}")
    value = factory.Faker("word")
