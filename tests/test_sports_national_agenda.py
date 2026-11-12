"""Exact representative-team aliases; no internet, fixture or broadcaster rules."""
import unittest

from scripts.sports_national_agenda import (ALIASES, CONMEBOL_TEAMS, UEFA_TEAMS,
                                            confederation, national_key, senior_pair)


class SportsNationalAgendaTests(unittest.TestCase):
    def test_uefa_and_conmebol_catalog_scope(self):
        self.assertEqual(55, len(UEFA_TEAMS))
        self.assertEqual(10, len(CONMEBOL_TEAMS))
        self.assertGreaterEqual(len(ALIASES), 100)
        self.assertEqual(set(), UEFA_TEAMS.intersection(CONMEBOL_TEAMS))

    def test_required_castilian_english_aliases(self):
        pairs = {'Argentina': 'Argentina', 'Brazil': 'Brasil', 'Brasil': 'Brasil',
                 'France': 'Francia', 'Italy': 'Italia', 'England': 'Inglaterra',
                 'Croatia': 'Croacia', 'Czech Republic': 'Chequia', 'Czechia': 'Chequia',
                 'República Checa': 'Chequia', 'Turkey': 'Turquía', 'Türkiye': 'Turquía',
                 'Holanda': 'Países Bajos', 'Paises Bajos': 'Países Bajos',
                 'Netherlands': 'Países Bajos', 'Kazajiztan': 'Kazajistán',
                 'Kazajistán': 'Kazajistán', 'Kazakhstan': 'Kazajistán',
                 'Irlanda': 'Irlanda', 'República Irlanda': 'Irlanda', 'Ireland': 'Irlanda'}
        for raw, canonical in pairs.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical, national_key(raw))

    def test_diacritics_whitespace_and_case_do_not_change_identity(self):
        self.assertEqual('Perú', national_key('  PERU  '))
        self.assertEqual('Países Bajos', national_key('  países    bajos '))
        self.assertEqual('Costa de Marfil', national_key("Côte d’Ivoire"))

    def test_observed_additional_aliases_remain_explicit(self):
        self.assertEqual('República Dominicana', national_key('Rep. Dominicana'))
        self.assertEqual('Bosnia y Herzegovina', national_key('Bosnia'))
        self.assertEqual('Macedonia del Norte', national_key('Macedonia Norte'))
        self.assertEqual('Irlanda del Norte', national_key('Northern Ireland'))

    def test_clubs_never_become_national_teams_by_partial_name(self):
        clubs = ('River Plate', 'Gimnasia La Plata', 'Argentina Juniors', 'Argentinos Juniors',
                 'Club Brasil', 'Brasil de Pelotas', 'Francia FC', 'Nacional',
                 'Colombia U. de Medellín', 'Nueva Zelanda FC', 'Inglaterra City')
        for name in clubs:
            with self.subTest(name=name):
                self.assertIsNone(national_key(name))
                self.assertIsNone(confederation(name))

    def test_youth_womens_reserve_olympic_and_b_variants_are_not_stripped(self):
        qualifiers = ('Sub-21', 'Sub21', 'Sub-20', 'U21', 'U-20', 'Under 21',
                      'Femenino', 'Femenina', 'Women', 'Womens', 'Reserva',
                      'Reserves', 'Olímpico', 'Olympic', 'B')
        for qualifier in qualifiers:
            for name in ('Argentina ' + qualifier, qualifier + ' France', 'Brasil (' + qualifier + ')'):
                with self.subTest(name=name):
                    self.assertIsNone(national_key(name))
                    self.assertIsNone(confederation(name))

    def test_unknown_names_and_invalid_types_are_not_guessed(self):
        for name in ('Atlántida', 'Argen', 'Brasill', 'England FC', 'Congo', 'Corea', '', None, 1, [], {}):
            with self.subTest(name=name):
                self.assertIsNone(national_key(name))
                self.assertIsNone(confederation(name))

    def test_both_senior_teams_must_be_known_and_different(self):
        self.assertTrue(senior_pair('Argentina', 'Uruguay'))
        self.assertTrue(senior_pair('Japan', 'Brazil'))
        self.assertFalse(senior_pair('Argentina', 'River Plate'))
        self.assertFalse(senior_pair('River Plate', 'Gimnasia La Plata'))
        self.assertFalse(senior_pair('Brazil', 'Brasil'))
        self.assertFalse(senior_pair('Argentina U21', 'Uruguay'))

    def test_regional_non_confederation_selection_is_not_a_senior_country(self):
        self.assertIsNone(national_key('País Vasco'))
        self.assertFalse(senior_pair('País Vasco', 'Venezuela'))

    def test_confederation_does_not_mix_uefa_and_concacaf(self):
        for name in ('Chequia', 'Turkey', 'Netherlands', 'Irlanda'):
            self.assertEqual('UEFA', confederation(name))
        for name in ('México', 'Guatemala', 'Panamá', 'Guadalupe', 'Curazao', 'Sint Maarten'):
            self.assertEqual('CONCACAF', confederation(name))
        self.assertEqual('CONMEBOL', confederation('Brasil'))
        self.assertEqual('AFC', confederation('Japan'))
        self.assertEqual('CAF', confederation('Morocco'))
        self.assertEqual('OFC', confederation('New Zealand'))

    def test_every_declared_alias_has_one_canonical_identity_and_confederation(self):
        for canonical, aliases in ALIASES.items():
            for alias in (canonical, *aliases):
                with self.subTest(alias=alias):
                    self.assertEqual(canonical, national_key(alias))
                    self.assertIsNotNone(confederation(alias))


if __name__ == '__main__':
    unittest.main()
