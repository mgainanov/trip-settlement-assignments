"""Regression expectations are isolated from imports and the accounting engine."""
import copy
import json
import random
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

import normalize
import settle

STAGE = Path(__file__).resolve().parents[1]
ROOT = STAGE.parent


class ExchangeRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            'own': ROOT / 'trip-001',
            'gist': STAGE / '.cache/sources/alexsunder-gist',
            'beijing': STAGE / '.cache/sources/hackathon-stages/w1-trip-beijing',
            'tbilisi': STAGE / '.cache/sources/trip-settle-tbilisi',
        }
        cls.data = {name: normalize.snapshot(normalize.load(path)) for name, path in cls.sources.items()}

    def dataset(self, name):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'normalized.json'
            path.write_text(json.dumps(self.data[name], default=str))
            return normalize.load_snapshot(path)

    def test_own_inputs_invariants_without_answer_key(self):
        result = settle.solve(self.dataset('own'))
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(sum(result['balances_cents'].values()), 0)
        self.assertEqual(sum(result['shares_cents'].values()), result['allocated_total_cents'])
        self.assertLessEqual(len(result['plan']), len(result['people']) - 1)
        self.assertTrue(result['final_settlement_ready'])

    def test_beijing_controls_and_fx_dates(self):
        result = settle.solve(self.dataset('beijing'))
        self.assertEqual(result['cash_total_cents'], 9634000)
        self.assertEqual(result['balances_cents'], {'Макс': 418000, 'Оля': -418000, 'Дима': -47720, 'Катя': 0, 'Саша': 47720})
        rows = {r['id']: r for r in result['rows']}
        self.assertNotIn('Саша', rows['R11']['shares'])
        self.assertIn('Саша', rows['R13']['shares'])
        self.assertEqual(rows['R07']['rate'], '11.95')
        self.assertEqual(rows['R15']['rate'], '12.05')
        self.assertEqual(rows['R10']['parent'], 'R08')
        self.assertEqual(result['minimum_transfers'], 2)
        self.assertTrue(result['final_settlement_ready'])

    def test_gist_source_appendix_matches_raw_repository(self):
        gist = settle.solve(self.dataset('gist'))
        beijing = settle.solve(self.dataset('beijing'))
        self.assertEqual(gist['balances_cents'], beijing['balances_cents'])
        self.assertEqual(gist['cash_total_cents'], beijing['cash_total_cents'])
        self.assertFalse(gist['final_settlement_ready'])
        self.assertTrue(any(i['kind'] == 'source_context_incomplete' for i in gist['disputes']))

    def test_tbilisi_unresolved_claims_not_silently_allocated(self):
        result = settle.solve(self.dataset('tbilisi'))
        self.assertEqual(result['cash_total_cents'], 9000000)
        self.assertEqual(result['allocated_total_cents'], 8900000)
        self.assertEqual(result['pending_allocation_cents'], 100000)
        self.assertEqual({i['kind'] for i in result['disputes']}, {'missing_amount', 'unconfirmed_transfer', 'disputed_allocation'})
        self.assertEqual(len(result['disputes']), 3)
        self.assertEqual(result['known_transfers'], [])
        self.assertFalse(result['final_settlement_ready'])
        rows = {r['id']: r for r in result['rows']}
        self.assertNotIn('Ира', rows['R06']['shares'])
        self.assertNotIn('Борис', rows['R10']['shares'])

    def test_tbilisi_author_scenario_is_explicitly_provisional(self):
        result = settle.solve(self.dataset('tbilisi'), 'equal-provisional')
        self.assertEqual(result['allocated_total_cents'], 9000000)
        self.assertEqual(result['balances_cents']['Аня'], 2435900)
        self.assertTrue(any(t['sender'] == 'Глеб' and t['recipient'] == 'Аня' and t['cents'] == 384200 for t in result['plan']))
        self.assertFalse(result['final_settlement_ready'])

    def test_duplicate_messages_and_no_extra_refund_count(self):
        for name, receipt in [('own', 'R07'), ('beijing', 'R04'), ('gist', 'R04'), ('tbilisi', 'R02')]:
            result = settle.solve(self.dataset(name))
            self.assertEqual(sum(r['id'] == receipt for r in result['rows']), 1)
            self.assertTrue(any(d['receipt'] == receipt for d in result['duplicates']))
        self.assertFalse(any(d['receipt'] == 'R10' for d in settle.solve(self.dataset('own'))['duplicates']))

    def test_ocr_without_answer_transcript(self):
        self.assertIn('TOTAL CNY 260.00', self.data['beijing']['ocr']['R04'])
        for data in self.data.values():
            text = json.dumps(data, default=str)
            self.assertNotIn('answers/', text)
            self.assertNotIn('facts.md', text)
            self.assertNotIn('construction-ledger', text)

    def test_published_foreign_results_reproduce_exactly(self):
        import sys
        sys.path.insert(0, str(STAGE))
        import run_exchange
        manifest = run_exchange.read_manifest()
        for name, scenario in [('gist', 'confirmed'), ('beijing', 'confirmed'), ('tbilisi', 'confirmed'), ('tbilisi-equal-provisional', 'equal-provisional')]:
            source = 'tbilisi' if name.startswith('tbilisi') else name
            actual = run_exchange.web_citations(settle.solve(self.dataset(source), scenario), STAGE / '.cache/sources', manifest)
            expected = json.loads((STAGE / 'results' / name / 'result.json').read_text())
            self.assertEqual(actual, expected, name)

    def test_published_source_urls_are_preserved_in_markdown(self):
        url = 'https://example.com/receipt.md#L12'
        self.assertIn('](' + url + ')', settle.source_link(url, STAGE))


class AccountingEdgeCases(unittest.TestCase):
    def dataset(self):
        ds = normalize.Dataset(Path('/synthetic-test'))
        ds.people = ['Первый', 'Второй', 'Третий']
        ds.receipts = [normalize.Receipt('R01', '2026-01-01', 'Первый', D('1.00'), 'RUB', 'Продавец', 'Общее', ds.people.copy(), '/synthetic-test/receipt.md')]
        return ds

    def test_rounding_exact_with_uneven_weights_and_refund(self):
        ds = self.dataset()
        ds.receipts[0].weights = dict(zip(ds.people, [D('.20'), D('.30'), D('.50')]))
        ds.receipts.append(normalize.Receipt('F01', '2026-01-02', 'Первый', D('-.33'), 'RUB', 'Продавец', 'Возврат', ds.people.copy(), '/synthetic-test/refund.md', parent='R01'))
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], 67)
        self.assertEqual(sum(result['shares_cents'].values()), 67)
        self.assertTrue(all(result['checks'].values()))

    def test_missing_date_rate_is_not_filled_from_another_day(self):
        ds = self.dataset()
        ds.receipts[0].currency = 'CNY'
        ds.rates['2026-01-02', 'CNY'] = D('12')
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], 0)
        self.assertTrue(any(i['kind'] == 'missing_rate' for i in result['disputes']))
        self.assertFalse(result['final_settlement_ready'])

    def test_refund_fx_can_exceed_original_rub_value_but_not_native_amount(self):
        ds = self.dataset()
        ds.receipts[0].currency = 'CNY'
        ds.rates = {('2026-01-01', 'CNY'): D('10'), ('2026-01-02', 'CNY'): D('12')}
        ds.rate_sources = {key: '/synthetic-test/rates.csv' for key in ds.rates}
        ds.receipts.append(normalize.Receipt('F01', '2026-01-02', 'Первый', D('-1'), 'CNY', 'Продавец', 'Возврат', ds.people.copy(), '/synthetic-test/refund.md', parent='R01'))
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], -200)
        self.assertTrue(all(result['checks'].values()))

    def test_native_refund_limit(self):
        ds = self.dataset()
        ds.receipts.append(normalize.Receipt('F01', '2026-01-02', 'Первый', D('-1.01'), 'RUB', 'Продавец', 'Возврат', ds.people.copy(), '/synthetic-test/refund.md', parent='R01'))
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], 100)
        self.assertTrue(any(i['kind'] == 'excess_refund' for i in result['disputes']))

    def test_refund_duplicate_combined_and_conflicting_identity_excluded(self):
        ds = self.dataset()
        refund = normalize.Receipt('F01', '2026-01-02', 'Первый', D('-.20'), 'RUB', 'Продавец', 'Возврат', ds.people.copy(), '/synthetic-test/refund.md', parent='R01')
        ds.receipts.extend([refund, copy.deepcopy(refund)])
        normalize.finalize(ds)
        self.assertEqual(settle.solve(ds)['cash_total_cents'], 80)
        ds = self.dataset()
        conflict = copy.deepcopy(ds.receipts[0])
        conflict.amount = D('2')
        ds.receipts.append(conflict)
        normalize.finalize(ds)
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], 0)
        self.assertTrue(any(i['kind'] == 'duplicate_conflict' for i in result['disputes']))

    def test_organizer_claim_disagrees_with_receipt(self):
        ds = self.dataset()
        ds.messages = [normalize.Message('M01', '2026-01-01', 'Первый', 'Оплатил 2 RUB, чек R01', '/synthetic-test/chat.md#m01')]
        normalize.finalize(ds)
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], 100)
        self.assertTrue(any(i['kind'] == 'amount_mismatch' for i in result['disputes']))
        self.assertFalse(result['final_settlement_ready'])

    def test_confirmed_transfer_changes_balances_not_expense_total(self):
        ds = self.dataset()
        ds.messages = [normalize.Message('M01', '2026-01-01', 'Второй', 'Первый, уже перевёл 0.10 RUB', '/synthetic-test/chat.md#m01'), normalize.Message('M02', '2026-01-01', 'Первый', 'Получил 0.10 RUB', '/synthetic-test/chat.md#m02')]
        normalize.finalize(ds)
        result = settle.solve(ds)
        self.assertEqual(result['cash_total_cents'], 100)
        self.assertEqual(result['balances_cents']['Второй'], -23)
        self.assertEqual(len(result['known_transfers']), 1)

    def test_minimum_uses_zero_sum_subgroups(self):
        plan, minimum = settle.plan_transfers({'A': -800, 'B': -700, 'C': 700, 'D': 800})
        self.assertEqual(minimum, 2)
        self.assertEqual(len(plan), 2)

    def test_random_exact_allocations_and_plan_closure(self):
        rng = random.Random(42)
        for _ in range(100):
            amount = rng.randrange(1, 100000)
            weights = {str(i): D(rng.randrange(1, 100)) for i in range(5)}
            shares = settle.allocate(amount, weights, list(weights))
            self.assertEqual(sum(shares.values()), amount)
            balances = {n: -s for n, s in shares.items()}
            balances['0'] += amount
            plan, minimum = settle.plan_transfers(balances)
            for t in plan:
                balances[t['sender']] += t['cents']
                balances[t['recipient']] -= t['cents']
            self.assertEqual(sum(abs(v) for v in balances.values()), 0)
            self.assertEqual(len(plan), minimum)


if __name__ == '__main__':
    unittest.main(verbosity=2)
