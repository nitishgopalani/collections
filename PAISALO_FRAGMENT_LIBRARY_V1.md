# PaisaLo Fragment Library v1 — Compose Lane Seed
_Out-of-flow जवाबों की approved library · Draft for Fonada review → PaisaLo sign-off · 09 Aug 2026_

## Rendering rules (engine contract — पढ़े बिना fragments मत जोड़ना)
1. **हर compose reply = ≤2 fragments + अपने-आप जुड़ा pending re-ask.** Resume renderer जोड़ता है; fragment में कभी "तो क्या आप payment…" मत लिखो।
2. **(रही/रहा) = persona-gender variant.** Renderer voice से चुनता है: priya/neha → रही, kabir/amit → रहा। Fragment text में `{G:रही|रहा}` token।
3. `{slot}` = hydrated fact। **Fragment सिर्फ़ अपने `slots` column के slots use कर सकता है** — grounding by construction।
4. **Amounts हमेशा `{X} रुपये` form में** (TTS "45 सौ" नहीं बोलेगा)। Phone numbers शब्दों में अंक-दर-अंक।
5. पूरी library **offline compliance-gate pass** करती है deploy से पहले (P5.0-style dry-run); allowlist-hit fragments marked हैं।
6. `safe_in`: **Q** = सिर्फ़ सवाल के जवाब में · **D** = demand/complaint context में भी safe · fragments जो commitment जैसे सुनाई दें, deliberately excluded (नीचे §Exclusions)।
7. Selection prompt LLM को `answers` tags दिखाता है — paid-vs-due class-mismatch इन्हीं tags से रुकता है।

Legend: 🆕 = नया data-feed field चाहिए · ⚖️ = business decision pending (§Decisions) · 🔒 = allowlist-relevant

---

## A. इस भुगतान के बारे में (facts)

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| fact_amount_due | आपकी कुल देय राशि {repay_amount} रुपये है और ड्यू डेट {due_date} है। | repay_amount, due_date | due_amount, due_date | Q+D | live-proven |
| fact_due_date | आपकी ड्यू डेट {due_date} है। | due_date | due_date | Q+D | |
| fact_amount_paid | हमारे रिकॉर्ड के अनुसार अब तक {amount_paid} रुपये जमा हुए हैं। | amount_paid | paid_total | Q | ⚠️ verify: seed/feed में field है? |
| fact_last_payment | आख़िरी भुगतान {last_payment_amount} रुपये, {last_date_paid} को प्राप्त हुआ था। | last_payment_amount, last_date_paid | last_payment | Q | |
| fact_total_outstanding | आपके लोन की कुल बकाया राशि {total_outstanding} रुपये है। | total_outstanding 🆕 | outstanding_total | Q | "पूरा कितना बचा" — बहुत common |
| fact_emi_remaining | अभी {emi_remaining} किश्तें शेष हैं। | emi_remaining 🆕 | emi_count | Q | |
| fact_emi_amount | आपकी मासिक किश्त {emi_amount} रुपये है। | emi_amount 🆕 | emi_amount | Q | repay_amount से अलग जब arrears जुड़े हों |
| fact_dpd | यह राशि {days_past_due} दिनों से देय है। | days_past_due | dpd | Q+D | postdue/NPA only |
| fact_penalty_pre | समय पर भुगतान करने पर कोई अतिरिक्त शुल्क नहीं लगेगा। | — | penalty | Q | scenario=predue/ondue only |
| fact_penalty_post | देरी पर लागू शुल्क की सटीक जानकारी {branch} ब्रांच से मिल जाएगी। | branch | penalty | Q+D | number quote नहीं — feed में penalty नहीं आता ⚖️D-P1 |

## B. भुगतान कैसे करें (mechanics)

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| cap_payment_modes_abf | भुगतान केवल पैसालो के आधिकारिक QR कोड से या {branch} ब्रांच में जाकर करें। | branch | how_to_pay | Q+D | product=ABF |
| cap_payment_modes_mfi | भुगतान पैसालो के QR कोड से, {branch} ब्रांच में, या पैसालो कर्मचारी को नगद कर सकते हैं — कृपया किसी कर्मचारी के पर्सनल अकाउंट में भुगतान न करें। | branch | how_to_pay | Q+D | product=MFI · PDF-verbatim modes |
| cap_no_personal_account | कृपया किसी भी कर्मचारी या डीलर के पर्सनल अकाउंट में भुगतान न करें — वह पैसालो के सिस्टम में दर्ज नहीं होता। | — | payment_warning | Q+D | PDF-verbatim भाव |
| cap_qr_lost | QR कोड के लिए आप {branch} ब्रांच जाएँ — वहाँ नया QR मिल जाएगा। | branch | lost_qr | Q+D | |
| cap_no_qr_resend | मैं कॉल पर QR दोबारा नहीं भेज सकती/सकता, पर आपका पुराना QR मान्य है और ब्रांच से नया मिल जाएगा। | — | resend_qr | Q+D | {G} |
| cap_upi_info | पैसालो का QR ही UPI से स्कैन होता है — कोई अलग UPI आईडी नहीं है। | — | upi | Q | ⚖️D-P2 verify |
| cap_receipt | भुगतान दर्ज होते ही आपको पुष्टि मिल जाती है; रसीद ब्रांच से भी ले सकते हैं। | — | receipt | Q | ⚖️D-P2 verify actual flow |
| cap_no_cash_on_call | मैं कॉल पर भुगतान नहीं ले सकती/सकता — QR या ब्रांच ही सही माध्यम है। | — | pay_on_call | Q+D | {G} |
| fact_branch | आपकी ब्रांच {branch} है, पता — {branch_address}। | branch, branch_address | branch_where | Q+D | live-proven |
| fact_branch_phone | ब्रांच का संपर्क नंबर {branch_phone} है। | branch_phone 🆕 | branch_phone | Q+D | digit-words render |
| fact_branch_hours | ब्रांच {branch_hours} खुली रहती है। | branch_hours 🆕 | branch_hours | Q | feed न मिले तो deflect_branch |
| fact_payment_lag | अगर आपने अभी भुगतान किया है तो सिस्टम में अपडेट होने में थोड़ा समय लग सकता है — {branch} ब्रांच से पुष्टि हो जाएगी। | branch | just_paid | Q+D | mid-call paid claim |

## C. लोन के बारे में

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| fact_loan_summary | आपने {loan_amount} रुपये का ऋण {disbursal_date} को लिया था। | loan_amount, disbursal_date | which_loan | Q+D | PDF which-EMI का fact-आधा |
| fact_loan_ref | आपका लोन खाता नंबर {loan_ref} है। | loan_ref 🆕 | loan_number | Q | payment reference के लिए ज़रूरी |
| deflect_documents | एग्रीमेंट या स्टेटमेंट की कॉपी {branch} ब्रांच से मिल जाएगी। | branch | documents | Q+D | |
| fact_interest_deflect | ब्याज दर की सटीक जानकारी आपके लोन एग्रीमेंट में है — {branch} ब्रांच दिखा देगी। | branch | interest_rate | Q | ⚖️D-P3: disclose vs deflect |

## D. परिणाम / राहत (compliance-heavy)

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| fact_cibil_soft | समय पर भुगतान से आपकी क्रेडिट प्रोफ़ाइल अच्छी रहती है; देरी का असर सिबिल पर पड़ सकता है। 🔒 | — | cibil | Q | predue/ondue tone |
| fact_cibil_npa | आपका खाता एनपीए श्रेणी में है — इसका असर सिबिल प्रोफ़ाइल और भविष्य में लोन मिलने पर पड़ सकता है। 🔒 | — | cibil, npa | Q+D | scenario=npa · PDF-verbatim भाव |
| deflect_legal_specifics | क़ानूनी प्रक्रिया की विस्तृत जानकारी आपको आधिकारिक सूचना या {branch} ब्रांच से ही मिलेगी — मैं उसका अनुमान नहीं लगाऊँगी/लगाऊँगा। 🔒 | branch | legal_details | Q+D | {G} · threats improvise नहीं |
| policy_no_waiver | लोन माफ़ी केवल आधिकारिक माध्यम से होती है — किसी व्यक्ति या बाहरी स्रोत से नहीं। | — | waiver | Q+D | PDF-verbatim भाव |
| deflect_settlement | सेटलमेंट या री-स्ट्रक्चर की बात {branch} ब्रांच पर दस्तावेज़ों के साथ ही हो सकती है। | branch | settlement, restructure | Q+D | hardship का सबसे common रास्ता |
| deflect_recovery_agent | ⚖️D-P4 — approved wording pending। Placeholder: "इस बारे में सही जानकारी ब्रांच से मिलेगी; मेरी कॉल केवल आपकी किश्त के भुगतान में मदद के लिए है।" | branch | field_visit | Q+D | careless जवाब = RBI-threat |

## E. कॉलर / भरोसा / शिकायत

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| fact_caller_identity | मैं {persona_name}, पैसालो से बोल रही/रहा हूँ — आपकी ब्रांच {branch} की ओर से। | persona_name, branch | who_are_you | Q+D | live-proven shape · {G} |
| fact_company | पैसालो वही संस्था है जिससे आपने यह लोन लिया है। | — | what_is_paisalo | Q | |
| policy_ai_disclosure | ⚖️D-P5 — recommended: "मैं पैसालो की वर्चुअल सहायिका हूँ।" | — | are_you_robot | Q+D | सच बोलो — recorded-call risk |
| fact_number_source | आपका नंबर आपके लोन आवेदन के रिकॉर्ड से है। | — | how_got_number | Q+D | DPDP-clean wording ⚖️D-P6 confirm |
| cap_recording | जी हाँ, गुणवत्ता और सुरक्षा के लिए यह कॉल रिकॉर्ड होती है। | — | is_recorded | Q+D | |
| policy_recording_revoke | ⚖️D-P7 — proposed: "रिकॉर्डिंग बंद नहीं हो सकती; आप चाहें तो {branch} ब्रांच पर व्यक्तिगत रूप से बात कर सकते हैं।" + graceful close | branch | stop_recording | D | |
| fact_grievance | शिकायत के लिए पैसालो ग्रीवांस हेल्पलाइन {grievance_contact} पर संपर्क करें। | grievance_contact 🆕 | complaint_where | Q+D | **RBI FPC-mandated — must-have** |
| cap_verify_legitimacy | आप पैसालो के आधिकारिक नंबर पर कॉल करके या {branch} ब्रांच जाकर इस कॉल की पुष्टि कर सकते हैं। | branch | is_this_fraud | Q+D | vishing-safe: details देकर prove मत करो |
| never_ask_otp | ध्यान रखें — पैसालो कभी भी OTP, पिन या पासवर्ड नहीं माँगता। | — | otp_safety | Q+D | fraud-prevention gold; verify_legitimacy के साथ pair |
| cap_no_transfer | अभी मैं कॉल ट्रांसफ़र नहीं कर सकती/सकता — {branch} ब्रांच या हेल्पलाइन आपकी मदद करेगी। | branch | human_agent | Q+D | {G} |

## F. Ack / empathy / deflect (demand-safe by design)

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| ack_neutral | मैं आपकी बात समझ रही/रहा हूँ। | — | — (pair-only) | D | {G} · कभी अकेला नहीं — हमेशा deflect/fact के साथ |
| ack_difficulty | मुझे खेद है कि यह समय आपके लिए मुश्किल है। | — | — (pair-only) | D | PDF empathy भाव |
| deflect_branch_generic | इस विषय में {branch} ब्रांच आपकी पूरी मदद करेगी। | branch | anything_branch | Q+D | universal pair |
| deflect_helpline | आप पैसालो हेल्पलाइन {helpline_number} पर भी संपर्क कर सकते हैं। | helpline_number 🆕 | helpline | Q+D | digit-words |
| policy_stop_calls | आपकी यह रिक्वेस्ट दर्ज हो गई है — इस विषय की अंतिम पुष्टि आपको पैसालो से मिल जाएगी। | — | dnc | D | **fires ONLY when dialer-suppression live** — बिना उसके यह jhootha वादा है |

## G. Meta-conversation + confirms

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| meta_repeat | जी, मैं दोहरा देती/देता हूँ। | — | repeat | Q+D | renderer last-reply repeat जोड़ता है · {G} |
| meta_language | अभी मैं हिंदी में ही बात कर सकती/सकता हूँ। | — | language | Q+D | ⚖️D-P8 अगर multilingual चाहिए · {G} |
| cap_no_sms | मैं SMS या WhatsApp नहीं भेज सकती/सकता — जानकारी ब्रांच या हेल्पलाइन से मिल जाएगी। | — | send_sms | Q+D | ⚖️D-P2 verify · {G} |
| meta_busy_short | बस तीस सेकंड का समय लूँगी/लूँगा। | — | busy | D | callback flow से पहले का soft ask · {G} |
| confirm_pay_today | यानी आप आज ही {repay_amount} रुपये का भुगतान कर देंगे — सही? | repay_amount | (gate) | — | Commitment-Gate downgrade output |
| confirm_pay_date | यानी आप {committed_date} तक {repay_amount} रुपये का भुगतान कर देंगे — सही? | committed_date, repay_amount | (gate) | — | readback-before-record |
| confirm_asked_paid | आप यह पूछ रहे हैं कि अब तक कितना जमा हुआ है — सही? | — | (gate) | — | paid-vs-due selection-uncertainty confirm |
| unknown_info | (tenant profile की मौजूदा line — terminal fallback) | branch | everything_else | Q+D | पहले से live |

## H. Dead-air apology (W1-B — H2 dead-air defense) — 🚧 PENDING-CLIENT-APPROVAL

Spoken via TTS when the media server detects an unrecoverable dead-air fault
(ASR reconnect exhausted → deaf call; or 2nd-consecutive TTS speak-fail → mute
call). Uses the tenant profile's `unknown_info`-register voice so the apology
matches the rest of the call's persona. The line is **terminal** — after
speaking it, the call clean-closes (`end_call=true`); no further turns run.

| id | Hindi text | slots | answers | safe_in | notes |
|---|---|---|---|---|---|
| apology_dead_air | माफ़ कीजिए, लाइन में तकनीकी समस्या आ रही है। हम आपसे थोड़ी देर में दोबारा संपर्क करेंगे। धन्यवाद। | — | (terminal) | D | **candidate #55 · PENDING-CLIENT-APPROVAL** · fires on `asr_dead=true` (ASR reconnect exhausted) or 2nd-consecutive TTS speak-fail · spoken in tenant `unknown_info` voice · clean-close after · SOT variant TBD (own copy, same register) |

> **Status:** PaisaLo draft above is the implementer's proposed copy. Nitish /
> PaisaLo must approve the exact wording before it is added to the YAML
> fragment manifest. Until then the engine uses this draft verbatim from the
> profile config (not the fragment library) so it can be hot-swapped without a
> redeploy. Mark approval in `docs/IMPLEMENTATION_TRACKER_V2.md` when confirmed.


---

## 🆕 Data-Feed Schema — PaisaLo से माँगने वाले नए fields
| Field | किस सवाल के लिए | ज़रूरत |
|---|---|---|
| total_outstanding | "पूरा कितना बचा?" | High |
| emi_remaining, emi_amount | "कितनी किश्तें/कितने की?" | High |
| loan_ref | "खाता नंबर?" + payment reference | High |
| grievance_contact | शिकायत (RBI-mandated) | **Compliance** |
| branch_phone | "ब्रांच का नंबर?" | High |
| branch_hours | timing | Medium |
| helpline_number | universal deflect | High |
| amount_paid / last_payment_* | seed-verify — schema में हैं? | High |

## ⚖️ Decisions (D-P1…P8) — एक-एक line का जवाब चाहिए
P1 penalty numbers disclose? · P2 UPI/receipt/SMS की असली capability क्या है? · P3 interest rate बोलें या deflect? · P4 recovery-agent approved line · P5 AI-disclosure (recommend सच) · P6 number-source wording legal-OK? · P7 recording-revoke script · P8 Hindi-only?

## Exclusions — जान-बूझकर fragments NAHI बनाए
- **"मैंने नोट कर लिया / शिकायत दर्ज कर ली"** — commitment जैसा सुनाई देता है (A2 risk)। जगह: ack_neutral + fact_grievance।
- Third-party / wrong-number / death / vulnerability / DNC-flow — ये **policy interrupts/flows** हैं, fragments नहीं।
- Legal-consequence threats — सिर्फ़ scripted PDF replies (allowlisted) बोलती हैं; compose lane में कोई threat fragment नहीं।
- PTP accept/counter — policy module का output, free selection नहीं।

## Expansion protocol (production data के साथ)
हर हफ़्ता: `unknown_info_rate` + escape-hatch transcripts cluster → नया cluster ≥N hits → 1 fragment draft → client approve (WhatsApp पर एक line) → YAML append → flows reload। Deploy नहीं चाहिए। Target: unknown_info_rate <5% by pilot week-3।

**गिनती: 51 fragments + unknown_info terminal · 8 नए feed-fields · 8 decisions · 4 exclusion classes।**



