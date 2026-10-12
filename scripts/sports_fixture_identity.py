"""Persistent provider-scoped identity. No fuzzy cross-provider transfers."""
import hashlib


class IdentityStore:
    def __init__(self, value=None):
        self.value = value if value is not None else {'schemaVersion': 1, 'entries': {}}
        if not isinstance(self.value, dict) or type(self.value.get('schemaVersion')) is not int or self.value.get('schemaVersion') != 1 or not isinstance(self.value.get('entries'), dict):
            raise ValueError('Invalid fixture identity store')
        self.reverse = {}
        for key, identifier in self.value['entries'].items():
            if not isinstance(key, str) or type(identifier) is not int or not 1_000_000_000 <= identifier <= 2_000_000_000 or identifier in self.reverse:
                raise ValueError('Invalid/colliding fixture identity')
            self.reverse[identifier] = key
        for alias, target in self.value.get('reconciliations', {}).items():
            if alias in self.value['entries'] or target not in self.value['entries']:
                raise ValueError('Invalid explicit identity reconciliation')

    def resolve(self, provider, provider_id):
        if not all(isinstance(v, str) and v.strip() for v in (provider, provider_id)):
            raise ValueError('Missing provider identity')
        key = provider + ':' + provider_id
        target = self.value.get('reconciliations', {}).get(key)
        if target is not None:
            return self.value['entries'][target]
        if key in self.value['entries']:
            return self.value['entries'][key]
        identifier = 1_000_000_000 + int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') % 1_000_000_001
        if identifier in self.reverse:
            raise ValueError('Fixture ID collision; explicit reconciliation required')
        self.value['entries'][key] = identifier
        self.reverse[identifier] = key
        return identifier

    def reconcile_provider(self, old_provider, old_id, new_provider, new_id, *, confirmed=False):
        """Explicit verified association only; never compare team names here."""
        if confirmed is not True:
            raise ValueError('Explicit confirmation required')
        old_key, new_key = old_provider + ':' + old_id, new_provider + ':' + new_id
        if old_key not in self.value['entries'] or new_key in self.value['entries']:
            raise ValueError('Identity cannot be reconciled automatically')
        self.value.setdefault('reconciliations', {})[new_key] = old_key

    def reconcile_kickoff(self, identifier, old_time, new_time, *, confirmed=False):
        if confirmed is not True or identifier not in self.reverse:
            raise ValueError('Explicit kickoff confirmation required')
        from .sports_auxiliary_policy import local_start
        item = [identifier, local_start(old_time).isoformat(), local_start(new_time).isoformat()]
        if item not in self.value.setdefault('kickoffReconciliations', []):
            self.value['kickoffReconciliations'].append(item)

    def kickoff_reconciled(self, identifier, old_time, new_time):
        from .sports_auxiliary_policy import local_start
        return [identifier, local_start(old_time).isoformat(), local_start(new_time).isoformat()] in self.value.get('kickoffReconciliations', [])
