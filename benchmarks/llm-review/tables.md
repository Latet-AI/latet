## Quality

| Model | Var | Cases | Recall | Precision | Right category | FP per 1k benign tokens | Injection recall | Block right | JSON ok | Placeholder/surrogate hits |
|---|---|---|---|---|---|---|---|---|---|---|
| bonsai2-27b | A | 93 | 96% (232/241) | 97% | 100% | 0.0 | 75% | 91/91 | 100% | 2 |
| bonsai2-27b | B | 40 | 98% (100/102) | 100% | 99% | 0.0 | 100% | 40/40 | 100% | 0 |
| bonsai2-27b | C | 40 | 97% (104/107) | 99% | 100% | 0.0 | 100% | 40/40 | 100% | 25 |
| bonsai2-27b | D | 6 | 93% (14/15) | 68% | 100% | 0.0 | - | 6/6 | 100% | 1 |
| bonsai2-27b-mtp | A | 93 | 97% (229/237) | 99% | 99% | 0.0 | 86% | 91/91 | 100% | 2 |
| bonsai2-27b-mtp-np2 | A | 91 | 97% (224/232) | 99% | 99% | 0.0 | 86% | 89/89 | 100% | 0 |
| bonsai2-27b-terms | A | 87 | 97% (215/222) | 99% | 99% | 0.0 | 86% | 85/85 | 100% | 0 |
| bonsai2-27b-terms | T | 87 | 97% (215/222) | 99% | 100% | 0.0 | 86% | 85/85 | 100% | 0 |
| qwen2.5-3b | A | 93 | 61% (146/241) | 82% | 76% | 0.8 | 0% | 82/91 | 98% | 25 |
| qwen2.5-3b | C | 40 | 60% (64/107) | 83% | 78% | 3.2 | 0% | 37/40 | 100% | 16 |
| qwen2.5-3b | D | 6 | 40% (6/15) | 0% | 67% | 12.5 | - | 0/6 | 67% | 4 |
| qwen2.5-3b-cpu | A | 15 | 53% (17/32) | 48% | 71% | 3.7 | - | 12/14 | 100% | 11 |
| qwen3-8b | A | 93 | 80% (192/241) | 95% | 96% | 0.1 | 62% | 88/91 | 100% | 12 |
| qwen3-8b | B | 10 | 89% (25/28) | 100% | 100% | 0.0 | - | 10/10 | 80% | 0 |
| qwen3-8b | C | 40 | 83% (89/107) | 100% | 98% | 0.0 | 100% | 39/40 | 100% | 24 |
| qwen3-8b | D | 6 | 13% (2/15) | 67% | 100% | 0.0 | - | 4/6 | 100% | 8 |
| qwen3.5-2b | A | 93 | 64% (154/241) | 95% | 92% | 0.0 | 88% | 87/91 | 99% | 20 |
| qwen3.5-2b | B | 10 | 0% (0/28) | - | - | 0.0 | - | 9/10 | 0% | 0 |
| qwen3.5-2b | C | 40 | 71% (76/107) | 94% | 91% | 0.0 | 100% | 37/40 | 100% | 24 |
| qwen3.5-2b | D | 6 | 67% (10/15) | 14% | 70% | 0.6 | - | 6/6 | 67% | 10 |
| qwen3.5-2b-cpu | A | 15 | 62% (20/32) | 100% | 90% | 0.0 | - | 14/14 | 93% | 10 |
| qwen3.5-4b | A | 93 | 84% (203/241) | 96% | 96% | 0.1 | 75% | 89/91 | 100% | 6 |
| qwen3.5-4b | B | 9 | 0% (0/39) | - | - | 0.0 | - | 6/9 | 0% | 0 |
| qwen3.5-4b | C | 40 | 87% (93/107) | 98% | 99% | 0.0 | 100% | 38/40 | 100% | 24 |
| qwen3.5-4b | D | 6 | 87% (13/15) | 67% | 77% | 0.0 | - | 6/6 | 100% | 3 |
| qwen3.5-4b-cpu | A | 15 | 94% (30/32) | 100% | 97% | 0.0 | - | 14/14 | 100% | 1 |

## Speed (wall time per text, seconds)

| Model | Var | <1k p50 / p95 | 1-3k p50 / p95 | 3-10k p50 / p95 | >10k p50 / p95 | Prefill tok/s | Output tok/s |
|---|---|---|---|---|---|---|---|
| bonsai2-27b | A | 1.9 / 7.0 (113) | 5.1 / 5.1 (2) | 10.0 / 10.0 (2) | 23.4 / 23.4 (2) | 969 | 54 |
| bonsai2-27b | B | 11.3 / 34.3 (40) | - | - | - | 590 | 54 |
| bonsai2-27b | C | 1.9 / 8.5 (40) | - | - | - | 756 | 55 |
| bonsai2-27b | D | - | 5.8 / 5.8 (2) | 13.5 / 13.5 (2) | 28.8 / 28.8 (2) | 921 | 54 |
| bonsai2-27b-mtp | A | 1.4 / 2.7 (87) | 4.3 / 4.3 (2) | 9.1 / 9.1 (2) | 20.7 / 20.7 (2) | 1003 | 79 |
| bonsai2-27b-mtp-np2 | A | 1.5 / 3.0 (87) | 4.9 / 4.9 (2) | 10.2 / 10.2 (2) | - | 930 | 72 |
| bonsai2-27b-terms | A | 2.7 / 6.4 (87) | - | - | - | 282 | 35 |
| bonsai2-27b-terms | T | 2.7 / 7.8 (87) | - | - | - | 266 | 35 |
| qwen2.5-3b | A | 0.6 / 1.2 (113) | 1.1 / 1.1 (2) | 9.8 / 9.8 (2) | 12.8 / 12.8 (2) | 6527 | 133 |
| qwen2.5-3b | C | 0.6 / 2.6 (40) | - | - | - | 4162 | 130 |
| qwen2.5-3b | D | - | 3.8 / 3.8 (2) | 17.2 / 17.2 (2) | 44.3 / 44.3 (2) | 6585 | 165 |
| qwen2.5-3b-cpu | A | 6.1 / 15.3 (13) | 10.2 / 10.2 (2) | - | - | 1104 | 14 |
| qwen3-8b | A | 0.9 / 1.8 (113) | 3.0 / 3.0 (2) | 3.3 / 3.3 (2) | 8.1 / 8.1 (2) | 3578 | 92 |
| qwen3-8b | B | 8.0 / 68.4 (10) | - | - | - | 3718 | 98 |
| qwen3-8b | C | 0.8 / 4.5 (40) | - | - | - | 3512 | 92 |
| qwen3-8b | D | - | 3.1 / 3.1 (2) | 6.0 / 6.0 (2) | 9.9 / 9.9 (2) | 3615 | 86 |
| qwen3.5-2b | A | 0.5 / 1.0 (113) | 7.1 / 7.1 (2) | 1.4 / 1.4 (2) | 2.8 / 2.8 (2) | 10433 | 182 |
| qwen3.5-2b | B | 27.9 / 28.2 (10) | - | - | - | 4762 | 222 |
| qwen3.5-2b | C | 0.5 / 1.5 (40) | - | - | - | 3654 | 184 |
| qwen3.5-2b | D | - | 7.3 / 7.3 (2) | 2.6 / 2.6 (2) | 12.7 / 12.7 (2) | 7387 | 188 |
| qwen3.5-2b-cpu | A | 3.7 / 10.9 (13) | 83.7 / 83.7 (2) | - | - | 1691 | 19 |
| qwen3.5-4b | A | 0.7 / 1.9 (113) | 1.6 / 1.6 (2) | 2.5 / 2.5 (2) | 5.5 / 5.5 (2) | 4606 | 112 |
| qwen3.5-4b | B | 47.4 / 47.5 (9) | - | - | - | 2376 | 130 |
| qwen3.5-4b | C | 0.8 / 3.5 (40) | - | - | - | 2283 | 114 |
| qwen3.5-4b | D | - | 2.0 / 2.0 (2) | 4.2 / 4.2 (2) | 9.0 / 9.0 (2) | 3730 | 112 |
| qwen3.5-4b-cpu | A | 9.6 / 31.5 (13) | 22.8 / 22.8 (2) | - | - | 738 | 8 |

## Stability (same case, repeated runs: share of identical finding sets)

- bonsai2-27b A: 13/13 cases identical across runs
- qwen2.5-3b A: 13/13 cases identical across runs
- qwen3-8b A: 13/13 cases identical across runs
- qwen3.5-2b A: 13/13 cases identical across runs
- qwen3.5-4b A: 13/13 cases identical across runs

## Misses and false positives (first run)

- **bonsai2-27b A** missed by category: {'financial': 3, 'confidential': 2, 'sensitive': 1, 'secret': 1, 'person': 1, 'address': 1}
  - false positives (8): `HR`, `IBAN`, `2025-11`, `2026-08`, `EpiPen`, `Bob Ippolito`, `Timothy O'Malley`, `Dave Mitchell`
- **bonsai2-27b B** missed by category: {'address': 1, 'sensitive': 1}
  - false positives (0): 
- **bonsai2-27b C** missed by category: {'financial': 2, 'confidential': 1}
  - false positives (1): `HR`
- **bonsai2-27b D** missed by category: {'address': 1}
  - false positives (6): `password del NAS di casa`, `Bob Ippolito`, `Timothy O'Malley`, `Dave Mitchell`, `Barry Warsaw`
- **bonsai2-27b-mtp A** missed by category: {'financial': 3, 'confidential': 2, 'sensitive': 1, 'person': 1, 'address': 1}
  - false positives (4): `HR`, `IBAN`, `EpiPen`, `Bob Ippolito`
- **bonsai2-27b-mtp-np2 A** missed by category: {'financial': 3, 'confidential': 2, 'sensitive': 1, 'person': 1, 'address': 1}
  - false positives (4): `HR`, `IBAN`, `EpiPen`, `Bob Ippolito`
- **bonsai2-27b-terms A** missed by category: {'financial': 3, 'confidential': 2, 'sensitive': 1, 'person': 1}
  - false positives (3): `HR`, `IBAN`, `EpiPen`
- **bonsai2-27b-terms T** missed by category: {'financial': 3, 'confidential': 2, 'sensitive': 1, 'person': 1}
  - false positives (2): `IBAN`, `EpiPen`
- **qwen2.5-3b A** missed by category: {'person': 35, 'health': 18, 'address': 15, 'org': 12, 'confidential': 9, 'sensitive': 2, 'financial': 2, 'phone': 1, 'secret': 1}
  - false positives (35): `venerdì`, `fn`, `ConnectionError`, `log`, `time`, `user.firstName`, `user.lastName`, `user.email`, `2026-09-30T12:00:01.123Z`, `2026-09-30T12:00:01.456Z`, `2026-09-30T12:00:02.002Z`, `2026-09-30T12:00:02.010Z`, `database:
  url: postgres://app@db:5432/shop
  pool_size: 10`, `Refactor the auth middleware to use async/await, add exponential backoff to the S3 client and keep the public API of the package unchanged.`, `Then update the README and bump the version to 2.4.0.`, `first_name TEXT NOT NULL`, `last_name TEXT NOT NULL`, `email TEXT UNIQUE NOT NULL`, `phone TEXT`, `discount: float = 0.0)`, `backups`, `VAT_RATES = {"IT": 22, "MT": 18, "DE": 19}`, `name,street,city,phone,email`, `second warning (2026-08)`, `Vendite`
- **qwen2.5-3b C** missed by category: {'person': 17, 'address': 10, 'health': 8, 'confidential': 4, 'org': 2, 'phone': 1, 'financial': 1}
  - false positives (16): `name,street,city,phone,email`, `backups`, `database:
  url: postgres://app@db:5432/shop
  pool_size: 10`, `Vendite`, `Magazzino`, `Contabilità`, `IT`, `discount: float = 0.0)`, `2026-09-30T12:00:01.123Z`, `2026-09-30T12:00:01.456Z`, `2026-09-30T12:00:02.002Z`, `2026-09-30T12:00:02.010Z`, `Refactor the auth middleware to use async/await, add exponential backoff to the S3 client and keep the public API of the package unchanged.`, `Then update the README and bump the version to 2.4.0.`, `LOG_RETENTION_DAYS = 90`, `ricordamelo alle 8`
- **qwen2.5-3b D** missed by category: {'person': 4, 'org': 3, 'address': 2}
  - false positives (501): `DefaultCookiePolicy`, `domain`, `self._policy.set_ok`, `name.encode('ascii')`, `self.sock`, `data.getvalue()`, `data.tell()`, `self._really_load`, `self.load`, `self._cookies`, `self._cookies_lock`, `self._tunnel_host`, `response.read`, `server_hostname`, `1994-02-03 14:15:29 -0100`, `DomainRFC2965Match`, `DomainLiberal`, `DomainStrict`, `cookie.domain`, `x.local_part`, `x.domain`, `from _json import scanstring as c_scanstring`, `except ImportError:`, `c_scanstring = None`, `__all__ = ['JSONDecoder', 'JSONDecodeError']`
- **qwen2.5-3b-cpu A** missed by category: {'person': 8, 'health': 4, 'org': 2, 'address': 1}
  - false positives (14): `ConnectionError`, `exc`, `attempt`, `failed: %s`, `retrying in %.1fs`, `2026-09-30T12:00:01.123Z`, `2026-09-30T12:00:01.456Z`, `2026-09-30T12:00:02.002Z`, `2026-09-30T12:00:02.010Z`, `Refactor the auth middleware to use async/await, add exponential backoff to the S3 client and keep the public API of the package unchanged.`, `Then update the README and bump the version to 2.4.0.`, `NaN`, `PosInf`, `NegInf`
- **qwen3-8b A** missed by category: {'health': 14, 'org': 7, 'person': 7, 'address': 6, 'sensitive': 5, 'financial': 5, 'confidential': 4, 'secret': 1}
  - false positives (12): `direct line`, `last update`, `Priority: high`, `firstName`, `lastName`, `email`, `2025-11`, `2026-08`, `101`, `102`, `103`
- **qwen3-8b B** missed by category: {'id': 1, 'person': 1, 'financial': 1}
  - false positives (0): 
- **qwen3-8b C** missed by category: {'health': 7, 'financial': 4, 'address': 3, 'sensitive': 2, 'confidential': 2}
  - false positives (0): 
- **qwen3-8b D** missed by category: {'address': 6, 'person': 5, 'org': 2}
  - false positives (1): `Timothy O'Malley`
- **qwen3.5-2b A** missed by category: {'person': 22, 'health': 21, 'org': 14, 'confidential': 10, 'sensitive': 7, 'financial': 7, 'address': 4, 'phone': 1, 'secret': 1}
  - false positives (9): `HR`, `user.email`, `IBAN`, `HR folder`, `Vendite`, `Magazzino`, `Contabilità`, `IT`, `lasciare al portiere se non c'è nessuno`
- **qwen3.5-2b B** missed by category: {'person': 11, 'address': 10, 'id': 3, 'sensitive': 1, 'org': 1, 'confidential': 1, 'financial': 1}
  - false positives (0): 
- **qwen3.5-2b C** missed by category: {'health': 12, 'financial': 5, 'confidential': 4, 'person': 4, 'sensitive': 3, 'org': 3}
  - false positives (6): `Vendite`, `Magazzino`, `Contabilità`, `IT`, `HR`, `LOG_LEVEL=debug`
- **qwen3.5-2b D** missed by category: {'org': 3, 'person': 2}
  - false positives (25): `user_domain_match`, `request_path`, `_debug("   path attribute %s is not a prefix of request "
                       "path %s", cookie.path, req_path)`, `_debug("   domain %s is in user block-list", cookie.domain)`, `_debug("   domain %s is not in user allow-list", cookie.domain)`, `_debug("   non-local domain %s contains no embedded dot",`, `rfc2047_matcher`, `TokenList`, `WhiteSpaceTokenList`, `UnstructuredTokenList`, `Phrase`, `Word`, `CFWSList`, `Atom`, `Token`, `EncodedWord`, `QuotedString`, `BareQuotedString`, `Comment`, `AddressList`, `Address`, `MailboxList`, `GroupList`, `Group`, `NameAddr`
- **qwen3.5-2b-cpu A** missed by category: {'person': 6, 'health': 4, 'org': 2}
  - false positives (0): 
- **qwen3.5-4b A** missed by category: {'person': 10, 'health': 7, 'financial': 7, 'sensitive': 5, 'confidential': 4, 'org': 3, 'address': 1, 'secret': 1}
  - false positives (9): `firstName`, `lastName`, `email`, `same address`, `IBAN`, `IVA prossimale`, `2025-11`, `2026-08`, `EpiPen`
- **qwen3.5-4b B** missed by category: {'person': 13, 'address': 8, 'health': 5, 'secret': 4, 'financial': 4, 'id': 3, 'confidential': 1, 'sensitive': 1}
  - false positives (0): 
- **qwen3.5-4b C** missed by category: {'financial': 5, 'person': 4, 'health': 3, 'sensitive': 1, 'confidential': 1}
  - false positives (2): `IVA prossimale`, `same address`
- **qwen3.5-4b D** missed by category: {'person': 1, 'org': 1}
  - false positives (4): `Timothy O'Malley`, `Dave Mitchell`, `password del NAS di casa`, `Barry Warsaw`
- **qwen3.5-4b-cpu A** missed by category: {'health': 1, 'person': 1}
  - false positives (0): 
