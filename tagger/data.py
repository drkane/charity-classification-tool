import warnings
from airtable import Airtable
import pandas as pd
from slugify import slugify

from flask.cli import AppGroup

from tagger import settings
warnings.filterwarnings("ignore", 'This pattern has match groups')

data_cli = AppGroup("data")

RESULT_TYPES = {
    "false-positive": "Records that should not have been selected but were",
    "false-negative": "Records that weren't selected but should have been",
    "true-positive": "Records that were correctly selected",
    "true-negative": "Records that were correctly not selected",
}

def prepare_completed_data(tags):
    airtable = Airtable(
        settings.AIRTABLE_BASE_ID,
        settings.AIRTABLE_SAMPLE_TABLE_NAME,
        settings.AIRTABLE_API_KEY,
    )
    data = airtable.get_all()
    data = pd.DataFrame(
        index=[i["id"] for i in data],
        data=[i["fields"] for i in data],
    )
    data.loc[:, settings.TAGS_FIELD_NAME] = data[settings.TAGS_FIELD_NAME].apply(lambda taglist: [tags.get(x, x) for x in taglist])
    data.to_pickle(settings.COMPLETED_DF)
    return data


def get_completed_data():
    data = pd.read_pickle(settings.COMPLETED_DF)
    corpus = pd.DataFrame([data["name"], data["activities"].fillna(data["objects"])]).T.apply(
        lambda x: " ".join(x), axis=1
    )
    return (data, corpus)


def group_by_with_total(df, column="income_band"):
    gb = df[column].value_counts()
    if gb.index.is_categorical():
        gb.index = gb.index.add_categories("Total")
    gb["Total"] = gb.sum()
    return gb


def get_all_charities(keyword_regex, exclude_regex, sample_size=20):
    df = pd.read_pickle(settings.ALL_CHARITIES_DF)
    stats = pd.read_pickle(settings.ALL_CHARITIES_BY_INCOME_DF)

    # Get stats for all charities
    all_charities_by_income = group_by_with_total(df, "income_band")
    all_charities_count = len(df)

    # Reduce to just the matched charities
    corpus = pd.DataFrame([df["name"], df["activities"]]).fillna("").T.apply(
        lambda x: " ".join(x), axis=1
    )
    selected_items = corpus.str.contains(keyword_regex, regex=True, case=False)
    if exclude_regex and not pd.isna(exclude_regex):
        selected_items = selected_items & ~corpus.str.contains(exclude_regex, regex=True, case=False)
    df = df[selected_items]

    # get stats about the found charities
    found_charities = len(df)
    found_charities_by_income = group_by_with_total(df, "income_band")
    found_charities_by_income = (found_charities_by_income / all_charities_by_income)
    found_charities_by_income = pd.DataFrame({
        "percentage": found_charities_by_income,
        "estimated_total": found_charities_by_income * stats,
    })

    if found_charities <= sample_size:
        return df, found_charities_by_income
    else:
        return df.sample(sample_size), found_charities_by_income


def prepare_all_charities(completed=None):
    df = pd.read_csv(settings.ALL_CHARITIES_CSV)
    df.loc[:, "income_band"] = pd.cut(
        df["income"],
        [0,10000,100000,1000000,10000000,float("inf")],
        labels=["Under £10k", "£10k-£100k", "£100k-£1m", "£1m-£10m", "Over £10m"],
    )
    gb = group_by_with_total(df, "income_band")
    gb.to_pickle(settings.ALL_CHARITIES_BY_INCOME_DF)
    if isinstance(completed, pd.DataFrame):
        df = df[~df["reg_number"].isin(completed["reg_number"].unique())]
    df = df.sample(10000)
    df.loc[:, "activities"] = df["activities"].fillna(df["objects"])
    # reg_number,name,postcode,active,date_registered,date_removed,web,company_number,activities,objects,source,last_updated,income,spending,fye
    df = df[["reg_number", "name", "activities", "source", "income_band"]]
    df.to_pickle(settings.ALL_CHARITIES_DF)


def save_tags_used(df):
    df.to_pickle(settings.TAGS_USED_DF)


def get_tags_used():
    return pd.read_pickle(settings.TAGS_USED_DF)


@data_cli.command("initialise")
def initialise_data():
    print("initialising data")
    print("Fetching Tags")
    airtable = Airtable(
        settings.AIRTABLE_BASE_ID,
        settings.AIRTABLE_TAGS_TABLE_NAME,
        settings.AIRTABLE_API_KEY,
    )
    data = airtable.get_all()
    data = pd.DataFrame(
        index=[i["id"] for i in data],
        data=[i["fields"] for i in data],
    ).rename(columns={"Name": "tag"})
    data = data[data["Not used (describe why)"].isnull()]
    data.loc[:, "tag_slug"] = data["tag"].apply(slugify)
    data.loc[:, "precision"] = pd.NA
    data.loc[:, "recall"] = pd.NA
    data.loc[:, "f1score"] = pd.NA
    data.loc[:, "accuracy"] = pd.NA

    print("Fetching completed data")
    prepare_completed_data(data["tag"].to_dict())
    df, corpus = get_completed_data()
    print("Preparing all charities")
    prepare_all_charities(df)

    print("Finding used tags")
    tags_used = (
        df[settings.TAGS_FIELD_NAME]
        .apply(pd.Series)
        .unstack()
        .dropna()
        .value_counts()
        .rename("frequency")
    )
    data = data.join(tags_used, on="tag")

    print("Calculating regular expression results")
    for index, row in data[data["Regular expression"].notnull()].iterrows():
        result = get_keyword_result(row["tag"], row["Regular expression"], row.get("Exclude regular expression"), df, corpus)
        summary = get_result_summary(result)
        data.loc[index, "precision"] = summary["precision"]
        data.loc[index, "recall"] = summary["recall"]
        data.loc[index, "f1score"] = summary["f1score"]
        data.loc[index, "accuracy"] = summary["accuracy"]

    data = data.sort_values("frequency", ascending=False)
    save_tags_used(data)


def get_keyword_result(tag, keyword_regex, exclude_regex, df, corpus):
    selected_items = corpus.str.contains(keyword_regex, regex=True, case=False)
    if exclude_regex and not pd.isna(exclude_regex):
        selected_items = selected_items & ~corpus.str.contains(exclude_regex, regex=True, case=False)
    relevant_items = df[settings.TAGS_FIELD_NAME].apply(lambda x: tag in x if x else False)
    result = pd.DataFrame(
        {
            "selected": selected_items,
            "relevant": relevant_items,
        }
    )
    result.loc[result["selected"] & result["relevant"], "result"] = "true-positive"
    result.loc[result["selected"] & ~result["relevant"], "result"] = "false-positive"
    result.loc[~result["selected"] & ~result["relevant"], "result"] = "true-negative"
    result.loc[~result["selected"] & result["relevant"], "result"] = "false-negative"
    return result


def get_result_summary(result):
    result_summary = {
        "relevant": result["relevant"].sum(),
        "selected": result["selected"].sum(),
    }
    for r in RESULT_TYPES.keys():
        result_summary[r] = (result["result"] == r).sum()
    result_summary["precision"] = result_summary["true-positive"] / (
        result_summary["true-positive"] + result_summary["false-positive"]
    )
    result_summary["recall"] = result_summary["true-positive"] / (
        result_summary["true-positive"] + result_summary["false-negative"]
    )
    result_summary["f1score"] = 2 * (
        (result_summary["precision"] * result_summary["recall"])
        / (result_summary["precision"] + result_summary["recall"])
    )
    result_summary["accuracy"] = (
        result_summary["true-positive"] + result_summary["true-negative"]
    ) / len(result)
    return result_summary


def save_regex_to_airtable(tag_id, new_regex, exclude_regex):
    if not new_regex or new_regex == settings.DEFAULT_REGEX:
        return False
    airtable = Airtable(
        settings.AIRTABLE_BASE_ID,
        settings.AIRTABLE_TAGS_TABLE_NAME,
        settings.AIRTABLE_API_KEY,
    )
    record = airtable.update(
        tag_id,
        {
            "Regular expression": new_regex,
            "Exclude regular expression": exclude_regex,
        }
    )
    return True
