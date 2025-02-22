import argparse
import json
import logging
import sys
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from typing import Tuple, Dict

from kgextractiontoolbox.backend.database import Session
from kgextractiontoolbox.backend.models import Document, Tag, Tagger, DocTaggedBy
from kgextractiontoolbox.document.count import count_documents
from kgextractiontoolbox.document.document import TaggedDocument
from kgextractiontoolbox.document.extract import read_pubtator_documents
from kgextractiontoolbox.progress import print_progress_with_eta

BULK_LOAD_COMMIT_AFTER = 50000
PRINT_ETA_EVERY_K_DOCUMENTS = 100
UNKNOWN_TAGGER = ["Unknown", "unknown"]


def read_tagger_mapping(filename: str) -> Dict[str, Tuple[str, str]]:
    """
    Reads the tagger mapping from `filename`.

    :param str filename: Path to tagger mapping JSON file.
    :return: Dict with entity type as key and tuple consisting of tagger name and tagger version as value
    """
    with open(filename) as f:
        content = f.read()
    mapping = json.loads(content)
    return mapping


def get_tagger_for_enttype(tagger_mapping, ent_type):
    if not tagger_mapping or ent_type not in tagger_mapping:
        return UNKNOWN_TAGGER[0], UNKNOWN_TAGGER[1]
    else:
        return tagger_mapping[ent_type][0], tagger_mapping[ent_type][1]


def insert_taggers(*tagger_list):
    """
    Inserts the taggers from the list.

    :param tagger_list: List consisting of Pairs with tagger name and tagger version
    :return:
    """
    session = Session.get()
    for tagger in tagger_list:
        insert_stmt = insert(Tagger).values(
            name=tagger[0],
            version=tagger[1],
        )
        session.execute(insert_stmt)
    session.commit()


def document_bulk_load(path, collection, tagger_mapping=None, logger=logging, ignore_tags=True):
    """
    Bulk load a file in PubTator/JSON Format or a directory of PubTator/JSON files into the database.
    Iterate over PubTator/JSON documents and add Document, Tag and DocTaggedBy objects.
    :param str path: Path to file or directory
    :param str collection: Identifier of the collection (e.g., PMC)
    :param dict tagger_mapping: Mapping from entity type to tuple (tagger name, tagger version)
    :param ignore_tags: if true no tags will be inserted
    :param logging logger: a logging instance to be used
    :return:
    """
    session = Session.get()

    if tagger_mapping is None:
        logger.warning("No tagger mapping provided. Tags are ignored")

    logger.info('Bulk loading documents into database...')
    sys.stdout.write("Counting documents ...")
    sys.stdout.flush()
    n_docs = count_documents(path)
    sys.stdout.write("\rCounting documents ... found {}\n".format(n_docs))
    sys.stdout.flush()
    logger.info("Found {} documents".format(n_docs))

    logger.info('Retrieving document ids from database...')
    query = session.query(Document.id).filter_by(collection=collection)
    db_doc_ids = set()
    for r in session.execute(query):
        db_doc_ids.add(r[0])
    logger.info('{} documents are already inserted'.format(len(db_doc_ids)))
    start_time = datetime.now()

    document_inserts = []
    tag_inserts = []

    doc_tagged_by_inserts = []
    for idx, pubtator_content in enumerate(read_pubtator_documents(path)):
        doc = TaggedDocument(pubtator_content, ignore_tags=ignore_tags)
        tagged_ent_types = set()
        # Add document if its not already included
        if doc.id not in db_doc_ids and (doc.title or doc.abstract):
            db_doc_ids.add(doc.id)
            document_inserts.append(dict(
                collection=collection,
                id=doc.id,
                title=doc.title,
                abstract=doc.abstract,
            ))

        if doc.id not in db_doc_ids:
            logger.warning(
                "Document {} {} is not inserted into DB (no title and no abstract)".format(collection, doc.id))
        if doc.tags and not ignore_tags and doc.id in db_doc_ids:
            # Add tags
            for tag in doc.tags:
                tagged_ent_types.add(tag.ent_type)

                tag_inserts.append(dict(
                    ent_type=tag.ent_type,
                    start=tag.start,
                    end=tag.end,
                    ent_id=tag.ent_id,
                    ent_str=tag.text,
                    document_id=tag.document,
                    document_collection=collection,
                ))

            # Add DocTaggedBy
            for ent_type in tagged_ent_types:
                tagger_name, tagger_version = get_tagger_for_enttype(tagger_mapping, ent_type)
                doc_tagged_by_inserts.append(dict(
                    document_id=doc.id,
                    document_collection=collection,
                    tagger_name=tagger_name,
                    tagger_version=tagger_version,
                    ent_type=ent_type,
                ))

        if (idx + 1) % BULK_LOAD_COMMIT_AFTER == 0:
            session.bulk_insert_mappings(Document, document_inserts)
            session.bulk_insert_mappings(Tag, tag_inserts)
            session.bulk_insert_mappings(DocTaggedBy, doc_tagged_by_inserts)
            session.commit()

            document_inserts = []
            tag_inserts = []
            doc_tagged_by_inserts = []

        print_progress_with_eta("Adding documents", idx, n_docs, start_time, print_every_k=PRINT_ETA_EVERY_K_DOCUMENTS)

    logger.info(f'inserting {len(document_inserts)}')
    session.bulk_insert_mappings(Document, document_inserts)
    session.bulk_insert_mappings(Tag, tag_inserts)
    session.bulk_insert_mappings(DocTaggedBy, doc_tagged_by_inserts)
    session.commit()

    sys.stdout.write("\rAdding documents ... done in {}\n".format(datetime.now() - start_time))
    logger.info("Added {} documents in {}".format(n_docs, datetime.now() - start_time))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("-c", "--collection", required=True, help="Document collection name")
    parser.add_argument("-t", "--tagger-map", help="JSON file containing mapping from entity type "
                                                   "to tuple with tagger name and tagger version")
    parser.add_argument("--ignore_tags", action="store_true", help="Will ignore all tags in this document")
    parser.add_argument("--logsql", action="store_true", help='logs sql statements')
    args = parser.parse_args()

    tagger_mapping = None
    if args.tagger_map:
        tagger_mapping = read_tagger_mapping(args.tagger_map)
        tagger_list = list(tagger_mapping.values())
        tagger_list.append(UNKNOWN_TAGGER)
        insert_taggers(*tagger_list)

    if args.logsql:
        logging.basicConfig(format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
                            datefmt='%Y-%m-%d:%H:%M:%S',
                            level=logging.INFO)
        logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
    else:
        logging.basicConfig(format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
                            datefmt='%Y-%m-%d:%H:%M:%S',
                            level=logging.INFO)

    ignore_tags = True if args.ignore_tags else False
    document_bulk_load(args.input, args.collection, tagger_mapping, ignore_tags=ignore_tags)


if __name__ == "__main__":
    main()
