"""Feature dictionary v1: turn approved image categories into per-book model features.

Every feature is defined ONCE below (name, description, type, levels, source subcategory, attributes, rule,
before-release flag). `build_features` computes the value for every analysed book from the stored findings,
records timestamps (when the evidence image was captured, when it was extracted, when the feature was computed)
and writes:
  outputs/feature_dictionary.xlsx  - Features | Book features (wide) | Book features (long + timestamps) |
                                     Subcategories without feature | How to read
  outputs/model_features_wide.csv / model_features_long.csv
Each run is stored as a snapshot (tables feature_runs, book_features) so values can be tracked over time.

Rules are deterministic (regex on the extracted value + evidence text); they work on the LLM's findings now and
can later run on OCR text too. Sales bands are used ONLY to report coverage, never as a feature.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .config import Settings
from .db import now
from .util import log, write_csv, write_xlsx

DICTIONARY_VERSION = "v1 (2026-11-18, taxonomy v3.6)"
BANDS = ("Top", "Good", "Normal", "Bad")


@dataclass
class Feature:
    name: str
    description: str
    ftype: str                      # flag | ordinal | category | number | count
    subcategory: str                # "Category > Subcategory" it comes from ("(all images)" for image counts)
    attrs: tuple = ()
    rule: tuple = ()                # see _evaluate
    levels: tuple = ()
    before_release: str = "yes"     # yes | partly | risk
    source: str = "LLM now; OCR/rules possible"
    aggregation: str = "any image of the book"
    notes: str = ""


def _flag(name, desc, sub, attrs, pattern=None, **kw):
    return Feature(name, desc, "flag", sub, tuple(attrs), ("any", pattern), ("no", "yes"), **kw)


def _ordinal(name, desc, sub, attrs, steps, **kw):
    """steps: [(level, regex), ...] from low to high; level 0 = default when nothing matches."""
    return Feature(name, desc, "ordinal", sub, tuple(attrs), ("ordinal", steps),
                   tuple(s[0] for s in steps), aggregation="highest level over all images", **kw)


def _category(name, desc, sub, attrs, buckets, prefer=("front_cover",), **kw):
    return Feature(name, desc, "category", sub, tuple(attrs), ("category", buckets, prefer),
                   tuple(b[0] for b in buckets) + ("other",),
                   aggregation="most frequent bucket, cover findings first", **kw)


def _number(name, desc, sub, attrs, pattern=None, **kw):
    return Feature(name, desc, "number", sub, tuple(attrs), ("number", pattern),
                   aggregation="maximum over all images", **kw)


def _count(name, desc, sub, attrs, pattern=None, **kw):
    return Feature(name, desc, "count", sub, tuple(attrs), ("count", pattern),
                   aggregation="distinct matching findings", **kw)


def _images(name, desc, pattern, ftype="count", **kw):
    return Feature(name, desc, ftype, "(all images)", (), ("images", pattern),
                   ("no", "yes") if ftype == "flag" else (), source="code (image metadata)",
                   aggregation="images of the book", **kw)


MC = "Marketing Campaigns, Materials and Engagement"
TR = "Trade Offers and Ordering (bookseller-facing)"
BS = "Bestseller and Commercial Performance Claims"
AW = "Literary Awards and Recognition"
RP = "Reviews and Promotional Copy"
SM = "Social Media Profiles and Screenshots"
CE = "Collections, Editions and Product Display"
GT = "Genre, Themes and Intended Readers"
CD = "Cover and Promotional Graphic Design"
DP = "Depicted Characters, People and Subjects"
SO = "Scenes, Objects and Visual Action"
PF = "Publishing, Franchise and Series Identity"
BI = "Book Identification and Visible Credits"
EV = "Author and Book Events"
CH_ATTRS = ("marketing_activity", "marketing_platform", "marketing_campaign_label", "print_media_placement",
            "marketing_material_type", "promotional_format_label", "podcast_reference", "media_appearance")

FEATURES: list[Feature] = [
    # ---------------------------------------------------------------- marketing investment
    _ordinal("priority_level", "Publisher priority label printed on trade material.",
             f"{MC} > Publisher Priority and Campaign Size", ("marketing_priority_signal", "marketing_campaign_label", "promotional_format_label"),
             [("none", None), ("campaign", r"kampagne|campaign|marketing"),
              ("focus_title", r"schwerpunkt|empfehlungstitel|top-titel|highlight|focus"),
              ("lead_title", r"spitzentitel|lead title")]),
    _ordinal("campaign_size", "Announced size of the marketing campaign.",
             f"{MC} > Publisher Priority and Campaign Size", ("marketing_campaign_label", "marketing_priority_signal", "marketing_activity"),
             [("none", None), ("campaign", r"kampagne|campaign"),
              ("big_campaign", r"gro(ß|ss)e|large|big|umfangreich|extensive|reichweiten|high-reach|maxim|crossmedia|360")]),
    _flag("ch_social_ads", "Paid social / online ads (Meta, Google, Amazon, TikTok ads, performance campaign).",
          f"{MC} > Campaign Activities and Channels", CH_ATTRS,
          r"social|online|performance|meta|facebook|instagram|google|amazon ad|tiktok|pinterest|paid"),
    _flag("ch_influencer", "Influencer / creator / blogger / BookTok marketing.", f"{MC} > Campaign Activities and Channels",
          CH_ATTRS, r"influenc|blogger|creator|booktok|bookstagram|street team"),
    _flag("ch_out_of_home", "Out-of-home: posters, city-light, infoscreens, station or waiting-room TV, cinema.",
          f"{MC} > Campaign Activities and Channels", CH_ATTRS,
          r"out[- ]of[- ]home|ooh|infoscreen|city[- ]?light|clp|plakat|bahnhof|station|wartezimmer|fahrgast|kino|cinema|billboard|street poster"),
    _flag("ch_print_ads", "Print ads in newspapers or magazines.", f"{MC} > Campaign Activities and Channels",
          CH_ATTRS, r"print|anzeige|zeit|faz|magazin|magazine|newspaper|zeitung|spiegel ad"),
    _flag("ch_radio_podcast", "Radio, podcast or Spotify advertising.", f"{MC} > Campaign Activities and Channels",
          CH_ATTRS, r"radio|podcast|spotify|audio ad"),
    _flag("ch_reader_platforms", "Reader platforms: NetGalley, Vorablesen, LovelyBooks, reader groups.",
          f"{MC} > Campaign Activities and Channels", CH_ATTRS + ("promotional_format_label",),
          r"netgalley|vorablesen|lovelybooks|leserunde|reader discussion|lesejury|was liest du"),
    _flag("ch_book_fair", "Book fair presence (Frankfurt, Leipzig, fan events).", f"{MC} > Campaign Activities and Channels",
          CH_ATTRS + ("author_event", "event_name", "event_location"), r"buchmesse|book fair|messe|gamescom|spiel in essen|fan event"),
    _flag("ch_newsletter", "Newsletter marketing.", f"{MC} > Campaign Activities and Channels", CH_ATTRS, r"newsletter"),
    _count("n_channels", "Number of distinct campaign activities / channels listed.",
           f"{MC} > Campaign Activities and Channels", ("marketing_activity", "marketing_platform", "print_media_placement")),
    _flag("seasonal_campaign", "Seasonal campaign framing (Christmas, gift season, summer, Mother's Day).",
          f"{MC} > Campaign Activities and Channels", ("seasonal_marketing_signal", "marketing_campaign_label", "marketing_slogan", "marketing_activity"),
          r"weihnacht|christmas|advent|gift|geschenk|sommer|summer|muttertag|ostern|easter|valentin"),
    _flag("has_pos_material", "Point-of-sale material offered (display, poster, Aufsteller, sample packs).",
          f"{MC} > Point-of-Sale Materials", ("marketing_material_type", "marketing_material_category", "marketing_material_dimensions")),
    _flag("has_media_appearance", "TV, radio or podcast appearance / press focus announced.",
          f"{MC} > Media and PR Appearances", ("podcast_reference", "media_appearance", "broadcaster_logo", "tv_show_logo", "media_presence_claim"),
          before_release="partly"),
    _flag("has_qr_or_digital_content", "QR code or promoted digital content (reading sample, video, interview).",
          f"{MC} > Digital Content and QR Codes", ("qr_code_present", "qr_code_purpose", "promotional_content_offer"),
          r"^(?!.*\b(no|false|none)\b).*"),
    _flag("has_trade_incentive", "Incentives / contests / calls to action (mostly aimed at booksellers).",
          f"{MC} > Calls to Action and Incentives", ("promotional_prize", "promotional_campaign_mechanic", "marketing_call_to_action",
                                                     "submission_deadline", "advertised_deadline")),
    _flag("has_merchandise", "Book-related merchandise or licensed products.", f"{MC} > Merchandise and Licensed Products",
          ("merchandise_item",)),
    _flag("has_cause_marketing", "Donation / cause linked to sales.", f"{MC} > Campaign Activities and Channels",
          ("charity_donation_claim",)),
    # ---------------------------------------------------------------- trade offers
    _ordinal("reading_copy", "Reading copy offered to the trade (print Leseexemplar or digital via NetGalley).",
             f"{TR} > Reading Copies and Trade Formats", ("promotional_format_label", "marketing_platform", "marketing_activity", "promotional_content_offer"),
             [("none", None), ("print", r"leseexemplar|reading cop|review cop|arc\b"),
              ("digital", r"netgalley|e-?leseexemplar|digital(es)? (lese|reading|review)")]),
    _flag("has_order_package", "Trade order deal (Vorbestellpaket, Aktionspaket, x/y free copies, display packs).",
          f"{TR} > Pre-order and Action Packages", ("preorder_offer_signal", "preorder_package_notation", "packaging_unit",
                                                     "promotional_format_label", "trade_target_group"),
          r"paket|package|\d+\s*/\s*\d+|ve\b|vpe|display|stapel|bundle|preorder|vorbesteller|azubi"),
    _flag("has_binding_first_sale_date", "Binding first-sale date (embargo / 'verbindlicher EVT') printed for the trade.",
          f"{TR} > Release Timing", ("embargo_release_signal", "release_date"), r"verbindlich|embargo|binding|weltweit|international|evt"),
    # ---------------------------------------------------------------- claims and social proof
    _ordinal("bestseller_scope", "What a printed bestseller claim refers to (book-level claims are often added after success).",
             f"{BS} > Bestseller Status and Scope", ("bestseller_label", "bestseller_claim_scope", "bestseller_rank_claim"),
             [("none", None), ("author_or_previous", r"autor|author|previous|vorherig|series|reihe|serie|bestsellerautor"),
              ("this_book", r"this title|this book|title-level|advertised book|^\W*(spiegel[- ]?)?bestseller\W*$|platz|#1|no\. ?1|nr\.? ?1")],
             before_release="risk", notes="author-level claims are pre-release; 'this_book' is likely leakage"),
    _flag("bestseller_rank1", "Claim of rank 1 (Platz 1, #1, Nr. 1).", f"{BS} > Bestseller Status and Scope",
          ("bestseller_label", "bestseller_rank_claim"), r"platz 1\b|#1\b|no\. ?1\b|nr\.? ?1\b|nummer 1|number one",
          before_release="risk"),
    _category("bestseller_source", "Which list the bestseller claim cites.", f"{BS} > Bestseller Status and Scope",
              ("bestseller_label", "bestseller_source", "bestseller_geographic_scope"),
              [("spiegel", r"spiegel"), ("nyt_us", r"new york times|nyt|usa today|wall street|sunday times"),
               ("booktok", r"booktok|tiktok"), ("international", r"welt|world|international|dänemark|schwed|sweden|denmark")],
              prefer=(), before_release="partly"),
    _number("prior_sales_claim", "Largest copies-sold figure printed (earlier titles, series or author).",
            f"{BS} > Sales, Reach and Quantity Statements", ("prior_title_sales_claim", "sales_figure_claim"),
            r"sold|verkauf|cop(y|ies)|exemplar|bücher|books|auflage|million|mio"),
    _ordinal("award_status", "Award status printed for this book or its author.", f"{AW} > Award Identification and Status",
             ("award_name", "award_nomination", "award_shortlist", "award_year", "award_badge_style"),
             [("none", None), ("nominated", r"nomin|longlist"), ("shortlist", r"shortlist"),
              ("won", r"gewinner|winner|ausgezeichnet|preisträger|won\b|awarded")],
             before_release="risk", notes="nominations can be pre-release; 'won' usually after"),
    _flag("author_recognition", "Author presented as prize-winning, bestselling or renowned.",
          f"{AW} > Award Recipient and Author Recognition", ("author_award_claim", "author_recognition_claim", "award_claim_scope")),
    _count("n_review_quotes", "Number of printed review / endorsement quotes.", f"{RP} > Review Quotations and Sources",
           ("review_quote",), before_release="risk", notes="press quotes about THIS book usually appear after release"),
    _category("review_source_type", "Who the quotes come from.", f"{RP} > Review Quotations and Sources",
              ("review_attribution", "review_source", "review_quote_scope"),
              [("bookseller", r"buchhandl|bookseller|inhaber|buchladen"),
               ("press", r"zeit|faz|spiegel|süddeutsche|sz\b|ndr|wdr|deutschlandfunk|kultur|new yorker|times|guardian|figaro|taz|stern|magazin|presse|press|zeitung"),
               ("author_peer", r"autor|author|nobel|preisträger")], prefer=(), before_release="partly"),
    _flag("has_readalike_comparison", "'For fans of …' comparison with other authors or titles.", f"{RP} > Read-alike Comparisons",
          ("comparable_authors", "comparable_author_reference", "comparison_author_reference", "comparison_title")),
    _flag("is_debut", "Debut / first book claim.", f"{RP} > Debut, Novelty and Anniversary Claims",
          ("debut_book_claim", "new_release_signal", "promotional_claim"), r"debüt|debut|erstes buch|first book|erstling|first novel|erste[ns]? (kinder)?buch"),
    _flag("anniversary_tie_in", "Author, series or publisher anniversary used in marketing.",
          f"{RP} > Debut, Novelty and Anniversary Claims", ("anniversary_label", "edition_label"), r"jahre|jubil|anniversary|geburtstag"),
    _count("n_promotional_claims", "Number of distinct promotional claims / blurbs.", f"{RP} > Promotional Statements and Text Types",
           ("promotional_claim",)),
    # ---------------------------------------------------------------- author and reach
    _flag("author_social_presence", "Author / brand social media account shown.", f"{SM} > Account Identification",
          ("social_media_platform", "social_media_username", "social_media_display_name")),
    _number("max_social_followers", "Largest follower / subscriber number printed or visible.", f"{SM} > Reach, Metrics and Trends",
            ("social_media_follower_count", "advertised_reach"), r"follow|abonn|fans|subscri|^\W*[\d.,]+\s*(k|m|mio|tsd)?\W*$",
            before_release="partly", notes="screenshot date unknown - capture timestamp recorded"),
    _flag("has_trend_reference", "Hashtag / platform trend cited (#BookTok, #WitchTok, #tiktokmademebuyit).",
          f"{SM} > Reach, Metrics and Trends", ("social_media_trend_reference", "bestseller_label", "promotional_badge_style", "marketing_slogan"),
          r"#|booktok|tiktokmademebuyit|witchtok"),
    _flag("author_credential_shown", "Author's professional role, expertise or bio printed.", f"{BI} > Author Names and Roles",
          ("author_professional_role", "author_experience_claim", "author_biography")),
    _images("has_author_photo", "An author portrait was supplied.", r"author_portrait", ftype="flag"),
    _flag("has_author_events", "Readings, tours, signings or fair appearances announced.", f"{EV} > Event Identity and Participation",
          ("author_event", "event_name", "event_venue", "event_location", "event_date", "event_time"), before_release="partly"),
    # ---------------------------------------------------------------- product and edition
    _flag("has_sprayed_edges", "Sprayed / printed page edges (Farbschnitt).", f"{CE} > Edition and Format Features",
          ("edition_feature", "edition_label", "edition_finish"), r"farbschnitt|edge|schnitt"),
    _flag("is_special_edition", "Special, limited, numbered, deluxe or first-print-only edition.", f"{CE} > Edition and Format Features",
          ("edition_feature", "edition_label"), r"limit|sonder|special|deluxe|luxus|numbered|nummer|erstauflage|first print|first edition|collector|perfect edition"),
    _flag("has_premium_finish", "Premium finish (foil, embossing, metallic, padded cover).", f"{CE} > Edition and Format Features",
          ("edition_feature", "edition_finish", "edition_label"), r"foil|folie|präg|emboss|metall|gold|silber|silver|wattiert|padded|lack|varnish"),
    _flag("cover_is_provisional", "Image shows a provisional cover (data-quality flag, not a model feature).",
          f"{CE} > Edition and Format Features", ("cover_status_label", "cover_preview_label", "preliminary_cover_notice"),
          before_release="yes", notes="use to filter, not as predictor"),
    _flag("is_box_set_or_collection", "Box set, collected volume or anthology.", f"{CE} > Collection Format and Labels",
          ("collection_format_signal", "collection_label")),
    _number("series_volume", "Volume number within a series.", f"{PF} > Series and Narrative Cycles",
            ("collection_volume_number", "volume_numbers", "series_label"), r"band|vol|teil|book|#|^\W*\d+\W*$", source="also in VLB-TIX metadata"),
    _flag("has_media_tie_in", "Film, TV, game, franchise or podcast tie-in.", f"{PF} > Media Tie-ins and Organisations",
          ("media_tie_in_signal", "franchise_label")),
    _flag("is_translation", "Translated / internationally sold title.", f"{BI} > Translation and International Origin",
          ("translation_claim", "translation_signal", "original_edition_reference", "foreign_rights_claim")),
    _flag("has_digital_companion", "Online version, app or digital extra bundled.", f"{CE} > Digital Companions",
          ("digital_companion_claim", "digital_companion_platform", "required_accessory_notice")),
    _images("has_3d_mockup", "3D book mockup supplied (proxy for marketing effort).", r"mockup", ftype="flag"),
    _flag("has_quality_seal", "Publisher quality seal / promise.", f"{PF} > Publisher and Franchise Marks", ("publisher_quality_seal",)),
    # ---------------------------------------------------------------- cover look and genre
    _category("cover_type", "Main cover technique.", f"{CD} > Colour, Illustration and Lighting", ("illustration_style", "cover_composition"),
              [("photo", r"photo"), ("typographic", r"typograph|text-only|lettering"), ("collage", r"collage|composite"),
               ("illustration", r"illustrat|paint|drawing|cartoon|manga|anime|webtoon|vector|watercolou?r|comic|graphic|art")]),
    _category("cover_palette", "Dominant colour impression of the cover.", f"{CD} > Colour, Illustration and Lighting",
              ("cover_dominant_colours",),
              [("dark", r"black|dark|navy|charcoal|schwarz"), ("pastel", r"pastel|pale|light pink|mint|beige|cream|blush"),
               ("vivid", r"bright|neon|vivid|red|orange|yellow|magenta|turquoise"), ("light", r"white|light|grey|gray")]),
    _category("cover_subject_type", "What the cover mainly shows.", f"{DP} > Main Illustrated Subjects",
              ("main_visual_subject", "cover_main_figure", "cover_main_figures"),
              [("person", r"person|\bman\b|woman|girl|boy|couple|people|portrait|author|child|family|face|figure|hand|legs|silhouette|swimmer|cyclist|conductor|rider"),
               ("animal_creature", r"dragon|wolf|creature|monster|animal|\bcat\b|dog|bird|horse|fox|bear|sheep|heron|snail|lion|fish|owl|deer"),
               ("figure_armed", r"armou?r|warrior|knight|soldier|marine"),
               ("food", r"food|dish|cake|bread|vegetable|plate|bowl"),
               ("place", r"landscape|mountain|sea|forest|city|house|building|sky|river|valley|map|corridor|interior|room|door|street"),
               ("object", r"book|object|flower|plant|ball|\bcar\b|key|bottle|pencil|sandal|cup|chair"),
               ("abstract", r"abstract|pattern|shape|geometric|typograph|symbol|circle|ring|line|brushstroke")]),
    _category("printed_genre_label", "Genre word printed on the image.", f"{GT} > Explicit Genre Labels", ("genre_label",),
              [("roman", r"\broman\b|novel"), ("thriller", r"thriller"), ("krimi", r"krimi|crime|kriminal"),
               ("fantasy_romantasy", r"fantasy|romantasy"), ("romance", r"romance|liebes"), ("sachbuch", r"sachbuch|non-?fiction|essay|biograf|biograph"),
               ("ratgeber", r"ratgeber|guide|self-help|selbsthilfe"), ("kochbuch", r"koch|cook|back"),
               ("kinderbuch", r"kinder|bilderbuch|picture book|children"), ("comic_manga", r"comic|manga|graphic novel")], prefer=()),
    _category("visual_genre", "Genre the images signal (interpretation).", f"{GT} > Inferred Genre and Mood", ("genre_visual_signal",),
              [("romantasy_fantasy", r"romantasy|fantasy|dragon"), ("scifi", r"science|sci-fi|space|dystop"),
               ("thriller_crime", r"thriller|crime|krimi|mystery|detective|noir"), ("romance", r"romance|love|romcom|new adult"),
               ("literary_fiction", r"literary|contemporary fiction|novel|coming-of-age|historical fiction"),
               ("children", r"child|picture book|kids|middle grade"), ("ya", r"young adult|\bya\b"),
               ("cookbook_food", r"cook|food|baking|recipe|nutrition"), ("self_help_health", r"self-help|health|psycholog|parenting|guide|wellbeing|medical"),
               ("nonfiction_politics_history", r"politic|histor|econom|current affairs|biograph|memoir|non-fiction|nonfiction|essay|science"),
               ("hobby_travel_nature", r"travel|nature|garden|hobby|craft|knit|outdoor|sport|football"), ("comic_manga", r"comic|manga|graphic novel|webtoon")]),
    _category("visual_mood", "Mood the cover conveys (interpretation).", f"{GT} > Inferred Genre and Mood", ("visual_mood",),
              [("dark_suspense", r"dark|ominous|eerie|suspens|menac|sinister|tense|threat|myster|enigmatic|unsettl|brooding|surreal|atmospheric"), ("dramatic_epic", r"dramatic|epic|heroic|martial|intense"),
               ("romantic", r"romantic|tender|sensual|dreamy"), ("serious", r"serious|urgent|solemn|intellectual|confront"),
               ("calm_melancholic", r"calm|quiet|melanchol|contemplat|nostalg|reflective|serene"),
               ("cheerful_playful", r"cheer|playful|humor|fun|warm|friendly|light|joy|upbeat|optimis|whimsical|gentle"),
               ("bold_provocative", r"provoc|bold|direct|striking|confident|attention|energetic|high-energy|ironic|witty|pop\b"),
               ("informative", r"educational|technical|scientific|practical|documentary|informal|approachable|supportive|professional"),
               ("elegant", r"elegant|sophisticat|luxur|premium|prestig")]),
    _category("target_audience", "Intended readers (interpretation; trade-facing images excluded).", f"{GT} > Themes, Audiences and Approaches",
              ("target_audience_signal",),
              [("children", r"child|kinder|kids|picture book|parents reading"), ("young_adult", r"young adult|new adult|\bya\b|teen"),
               ("parents", r"parent|eltern"), ("professional", r"professional|student|exam|clinician|doctor|medical staff"),
               ("women", r"women|frauen"), ("fans_hobby", r"fans|hobby|home cook|gardener|knit|sport|cook"),
               ("adult_fiction", r"fiction|novel|thriller|crime|krimi|romance|literary|fantasy|roman"),
               ("adult_nonfiction", r"non-?fiction|interested|politic|histor|business|manager|health|self-help|science|culture"),
               ("general_adult", r"adult|general|readers|gift")]),
    _number("age_min", "Minimum reader age printed.", f"{GT} > Explicit Audience and Content Ratings", ("age_recommendation",),
            r"ab|from|age|jahre|years|\+"),
    _number("spice_level", "Printed spice / heat level (0-5).", f"{GT} > Explicit Audience and Content Ratings",
            ("spice_level_rating", "rating_scale", "content_intensity_signal"), r"level|of 5|chili|/5"),
    _flag("content_warning", "Printed content warning or 18+ notice.", f"{GT} > Explicit Audience and Content Ratings",
          ("content_warning_notice", "age_recommendation"), r"warn|18|content"),
    _flag("has_trope_marketing", "Trope list / moodboard used in marketing.", f"{GT} > Tropes and Moodboards", ("trope_label",)),
    # ---------------------------------------------------------------- image inventory
    _images("n_images_analysed", "Number of analysed images for the book.", r"."),
    _images("n_marketing_images", "Number of marketing images (banners, graphics, campaign sheets, social).",
            r"marketing|campaign|social|poster|promotional|catalogue|review_graphic"),
    _images("has_campaign_overview", "A campaign overview sheet was supplied.", r"campaign_overview", ftype="flag"),
]

NO_FEATURE = [  # (subcategory, reason)
    (f"{BI} > Titles and Related Works", "title text is metadata (VLB-TIX/Excel)"),
    (f"{BI} > Title and Story References", "content detail, too specific"),
    (f"{BI} > Contributors and Credits", "credits, not a sales signal"),
    (f"{CE} > Price, Identifiers and Contents", "price/ISBN/pages come from metadata"),
    (f"{CE} > Volume Designations", "covered by series_volume"),
    (f"{CE} > Physical Product Presentation", "covered by has_3d_mockup"),
    (f"{CD} > Book Cover Layout and Typography", "free-text layout descriptions; cover_type/palette used instead"),
    (f"{CD} > Promotional Graphic Layout", "design detail of banners"),
    (f"{CD} > Graphic Design Style Interpretation", "too subjective / sparse"),
    (f"{DP} > Secondary Illustrated Figures", "design detail"),
    (f"{DP} > Photographic Portraits", "covered by has_author_photo"),
    (f"{SO} > Illustrated Settings and Architecture", "optional later (cover_setting_type)"),
    (f"{SO} > Action, Weapons and Visual Effects", "rare (5%), genre-specific"),
    (f"{AW} > Award Badge Design", "visual style of badges"),
    (f"{BS} > Bestseller Badge Appearance", "visual style of badges"),
    (f"{RP} > Promotional Slogans and Formats", "free-text slogans"),
    (f"{SM} > Screenshot Appearance", "layout of screenshots"),
    (f"{TR} > Ordering Logistics and Contacts", "operational details (order numbers, e-mails)"),
    (f"{TR} > Bookseller Contests", "folded into has_trade_incentive"),
    (f"{EV} > Event Schedule and Venue", "folded into has_author_events"),
    ("Photographic and Reporting Context > (all)", "captions, people and photo credits - no sales signal"),
]

# ------------------------------------------------------------------------------------------------ evaluation
_NUM = re.compile(r"(\d+(?:[.,]\d+)*)\s*(mio\.?|millionen|million|mill\.?|m\b|k\b|tsd\.?|tausend|thousand)?", re.I)


def parse_quantity(text: str) -> float | None:
    """Largest number in a text; understands 1,2 Mio / 5.5 million / 415.3k / 325.000 / 44400."""
    best = None
    for m in _NUM.finditer(text or ""):
        num, unit = m.group(1), (m.group(2) or "").lower()
        if unit:
            val = float(num.replace(".", "").replace(",", ".") if re.fullmatch(r"\d{1,3}(\.\d{3})+", num)
                        else num.replace(",", "."))
            val *= 1_000_000 if unit.startswith(("m", "mio")) else 1_000
        else:
            if re.fullmatch(r"\d{1,3}([.,]\d{3})+", num):
                val = float(re.sub(r"[.,]", "", num))
            else:
                try:
                    val = float(num.replace(",", "."))
                except ValueError:
                    continue
        best = val if best is None else max(best, val)
    return best


def _text(f) -> str:
    v = f["value"] or ""
    try:
        v = json.loads(v)
        v = " ".join(map(str, v)) if isinstance(v, list) else str(v)
    except (ValueError, TypeError):
        pass
    return f"{v} {f['evidence'] or ''}".lower()


def _evaluate(feat: Feature, findings: list, images: list) -> tuple[object, list]:
    """Return (value, evidence findings/images) for one book."""
    kind = feat.rule[0]
    if kind == "images":
        hits = [i for i in images if re.search(feat.rule[1], i["image_type"] or "")]
        return ((len(hits) > 0) if feat.ftype == "flag" else len(hits)), hits
    pool = [f for f in findings if f["attribute"] in feat.attrs]
    if feat.attrs == ("target_audience_signal",):
        pool = [f for f in pool if not re.search(r"bookseller|buchhandel|trade", _text(f))]
    if kind == "any":
        pat = feat.rule[1]
        hits = [f for f in pool if not pat or re.search(pat, _text(f))]
        return len(hits) > 0, hits
    if kind == "ordinal":
        steps = feat.rule[1]
        best, hits = 0, []
        for f in pool:
            t = _text(f)
            for i in range(len(steps) - 1, 0, -1):
                if re.search(steps[i][1], t):
                    if i > best:
                        best, hits = i, [f]
                    elif i == best:
                        hits.append(f)
                    break
        return steps[best][0], hits
    if kind == "category":
        buckets, prefer = feat.rule[1], feat.rule[2]
        votes, hits = Counter(), defaultdict(list)
        for f in pool:
            t = _text(f)
            b = next((name for name, pat in buckets if re.search(pat, t)), "other")
            w = 3 if f["image_type"] in prefer else 1
            votes[b] += w
            hits[b].append(f)
        if not votes:
            return None, []
        top = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0] == "other"))[0][0]
        return top, hits[top]
    if kind == "number":
        pat = feat.rule[1]
        best, hits = None, []
        for f in pool:
            t = _text(f)
            if pat and not re.search(pat, t):
                continue
            q = parse_quantity(t)
            if feat.name == "age_min":
                ages = [float(x) for x in re.findall(r"\d+", t) if int(x) <= 21]
                q = min(ages) if ages else None
            if feat.name == "prior_sales_claim" and q is not None and q < 1000:
                continue
            if feat.name == "spice_level" and q is not None and q > 5:
                continue
            if feat.name in ("age_min", "series_volume") and q is not None and q > 99:
                continue
            if q is not None and (best is None or q > best):
                best, hits = q, [f]
        return best, hits
    if kind == "count":
        pat = feat.rule[1]
        vals = {_text(f) for f in pool if not pat or re.search(pat, _text(f))}
        return len(vals), [f for f in pool]
    raise ValueError(f"unknown rule {kind}")


def _is_set(feat: Feature, v) -> bool:
    if feat.ftype == "flag":
        return bool(v)
    if feat.ftype == "ordinal":
        return v not in (None, feat.levels[0])
    if feat.ftype in ("number", "count"):
        return bool(v)
    return v is not None


# ------------------------------------------------------------------------------------------------ build
def build_features(conn: sqlite3.Connection, settings: Settings) -> dict:
    books = [r["isbn13"] for r in conn.execute(
        "SELECT DISTINCT isbn13 FROM extractions WHERE is_current=1 ORDER BY isbn13")]
    if not books:
        raise ValueError("no analysed images yet - import extraction results first")
    meta = {r["isbn13"]: r for r in conn.execute("SELECT isbn13, title, publisher, publication_date FROM books")}
    band = dict(conn.execute("SELECT isbn13, band FROM sample_books").fetchall())
    fnd = defaultdict(list)
    for r in conn.execute(
            "SELECT f.isbn13, f.image_id, f.attribute, f.value, f.evidence, e.image_type, e.created_at AS extracted_at, "
            "i.downloaded_at FROM findings f JOIN extractions e ON e.extraction_id=f.extraction_id AND e.is_current=1 "
            "LEFT JOIN images i ON i.isbn13=f.isbn13 AND i.image_id=f.image_id"):
        fnd[r["isbn13"]].append(r)
    imgs = defaultdict(list)
    for r in conn.execute(
            "SELECT e.isbn13, e.image_id, e.image_type, e.created_at AS extracted_at, i.downloaded_at FROM extractions e "
            "LEFT JOIN images i ON i.isbn13=e.isbn13 AND i.image_id=e.image_id WHERE e.is_current=1"):
        imgs[r["isbn13"]].append(r)

    ts = now()
    run_id = conn.execute("INSERT INTO feature_runs(dictionary_version, computed_at, books, features) VALUES (?,?,?,?)",
                          (DICTIONARY_VERSION, ts, len(books), len(FEATURES))).lastrowid
    long_rows, wide_rows, db_rows = [], [], []
    coverage = defaultdict(Counter)            # feature -> band -> books with value
    band_totals = Counter(band.get(b, "(not in sample)") for b in books)
    for isbn in books:
        pub = (meta[isbn]["publication_date"] if isbn in meta else "") or ""
        observed = max((i["downloaded_at"] or "" for i in imgs[isbn]), default="") or None
        wide = [isbn, meta[isbn]["title"] if isbn in meta else "", meta[isbn]["publisher"] if isbn in meta else "", pub]
        for feat in FEATURES:
            value, ev = _evaluate(feat, fnd[isbn], imgs[isbn])
            has = _is_set(feat, value)
            if has:
                coverage[feat.name][band.get(isbn, "(not in sample)")] += 1
            ev_imgs = sorted({e["image_id"] for e in ev}) if has else []
            first_seen = min((e["downloaded_at"] for e in ev if e["downloaded_at"]), default=None) if has else None
            extracted = min((e["extracted_at"] for e in ev if e["extracted_at"]), default=None) if has else None
            before = None
            if first_seen and pub:
                try:
                    before = (date.fromisoformat(pub[:10]) - date.fromisoformat(first_seen[:10])).days
                except ValueError:
                    before = None
            shown = ("yes" if value else "no") if feat.ftype == "flag" else value
            wide.append(shown)
            long_rows.append([isbn, feat.name, shown, has, ",".join(ev_imgs), first_seen or "", observed or "",
                              extracted or "", ts, before if before is not None else "", pub])
            db_rows.append((run_id, isbn, feat.name, json.dumps(value, ensure_ascii=False, default=str), int(has),
                            ",".join(ev_imgs), first_seen, observed, extracted, ts))
        wide_rows.append(wide)
    conn.executemany(
        "INSERT INTO book_features(run_id, isbn13, feature, value, has_value, evidence_images, first_seen_at, "
        "observed_at, extracted_at, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?)", db_rows)
    conn.commit()

    n = len(books)
    dict_rows = []
    for feat in FEATURES:
        c = coverage[feat.name]
        tot = sum(c.values())
        dict_rows.append([feat.name, feat.description, feat.ftype, " / ".join(map(str, feat.levels)), feat.subcategory,
                          ", ".join(feat.attrs), feat.before_release, tot, round(100 * tot / n, 1),
                          *[round(100 * c[b] / band_totals[b], 1) if band_totals[b] else None for b in BANDS],
                          feat.source, feat.aggregation, feat.notes])
    out = settings.path(settings.outputs_dir)
    dict_head = ["Feature", "Description", "Type", "Levels", "From subcategory", "Attributes used", "Before release",
                 "Books with value", "% books", "% Top", "% Good", "% Normal", "% Bad", "Source", "Aggregation", "Notes"]
    wide_head = ["EAN", "Title", "Publisher", "Publication date"] + [f.name for f in FEATURES]
    long_head = ["EAN", "Feature", "Value", "Has value", "Evidence images", "First seen (image captured)",
                 "Book images last captured", "Extracted at", "Computed at", "Days captured before release",
                 "Publication date"]
    guide = [
        ["Dictionary", DICTIONARY_VERSION],
        ["Books", f"{n} books with analysed images (sales bands only used for the coverage columns)"],
        ["% books", "share of analysed books where the feature has a value (yes / level above 'none' / number > 0)"],
        ["Before release", "yes = normally available before publication; partly = depends on timing; risk = often added after success (leakage)"],
        ["First seen", "when the image with the evidence was downloaded from VLB-TIX by this pipeline"],
        ["Book images last captured", "latest capture of the book's images - for 'no' values this is when absence was observed"],
        ["Extracted at", "when the LLM extraction of that image was imported"],
        ["Days captured before release", "publication date minus first-seen date; negative = captured after release (cannot prove pre-release availability)"],
        ["Timestamps", "every run is stored as a snapshot (tables feature_runs / book_features); re-run later to see when features appear"],
    ]
    path = write_xlsx(out / "feature_dictionary.xlsx", [
        ("Features", dict_head, dict_rows),
        ("Book features (wide)", wide_head, wide_rows),
        ("Book features (long)", long_head, long_rows),
        ("Subcategories without feature", ["Subcategory", "Reason"], [list(x) for x in NO_FEATURE]),
        ("How to read", ["Item", "Meaning"], guide)])
    write_csv(out / "model_features_wide.csv", wide_head, wide_rows)
    write_csv(out / "model_features_long.csv", long_head, long_rows)
    after = sum(1 for r in long_rows if r[3] and r[9] != "" and r[9] < 0)
    log.info("feature run %d: %d books x %d features -> %s", run_id, n, len(FEATURES), path)
    return {"run_id": run_id, "dictionary": DICTIONARY_VERSION, "books": n, "features": len(FEATURES),
            "file": str(path), "csv": [str(out / "model_features_wide.csv"), str(out / "model_features_long.csv")],
            "values_captured_after_release": after,
            "top_coverage": sorted(((r[0], r[8]) for r in dict_rows), key=lambda x: -x[1])[:10]}
