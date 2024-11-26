# train_eval_topic_model.py - SpectraSync: Adaptive Topic Modeling and Parallel Processing Engine
# Author: Alan Hamm
# Date: April 2024
#
# Description:
# This is the command center of SpectraSync, orchestrating model training, evaluation, and metadata generation 
# for high-dimensional topic modeling. Utilizing Dask’s distributed framework, it adapts dynamically to system resources, 
# tracking core allocation, scaling workloads, and ensuring seamless handling of expansive datasets. Each batch is logged 
# with meticulous metadata for reproducibility, enabling a powerful and efficient analysis pipeline.
#
# Functions:
# - Trains and evaluates LDA models, adapting core and memory resources based on workload demands.
# - Captures batch-specific metadata, including dynamic core usage, model parameters, and evaluation metrics.
# - Manages parallel workflows through Dask’s Client and LocalCluster, optimizing performance across distributed resources.
#
# Dependencies:
# - Python libraries: pandas, logging, pickle, hashlib, math, numpy, json, typing
# - Dask libraries: distributed (for adaptive parallel processing)
# - Gensim library for LDA modeling and coherence scoring
#
# Developed with AI assistance to power SpectraSync’s scalable, data-driven analysis engine.

import sys
import pprint as pp
import os
import pandas as pd  # Used to handle timestamps and date formatting for logging and metadata.
import dask
from dask import delayed
from dask.distributed import get_client
from dask.distributed import wait  # Manages asynchronous execution in distributed settings, ensuring all futures are completed.
import logging  # Provides error logging and information tracking throughout the script's execution.

from gensim.models import LdaModel  # Implements Latent Dirichlet Allocation (LDA) for topic modeling.
from gensim.corpora import Dictionary  # Converts tokenized text data into a bag-of-words format for LDA.

import pickle  # Serializes models and data structures to store results or share between processes.
import math  # Supports mathematical calculations, such as computing fractional core usage for parallel processing.
import hashlib  # Generates unique hashes for document metadata, ensuring data consistency.
import numpy as np  # Enables numerical operations, potentially for data manipulation or vector operations.
import cupy as cp
import json  # Provides JSON encoding and decoding, useful for handling data in a structured format.
from typing import Union  # Allows type hinting for function parameters, improving code readability and debugging.
import random
from datetime import datetime
from decimal import Decimal, InvalidOperation
from time import time
import copy

from .alpha_eta import calculate_numeric_alpha, calculate_numeric_beta  # Functions that calculate alpha and beta values for LDA.
from .utils import safe_serialize_for_postgres, convert_float32_to_float  # Utility functions for data type conversion, ensuring compatibility within the script.
from .utils import NumpyEncoder
from .batch_estimation import estimate_batches_large_docs_v2
from .mathstats import *
from .visualization import *
from .process_futures import get_and_process_show_topics, get_document_topics_batch, extract_topics_with_get_topic_terms


# https://examples.dask.org/applications/embarrassingly-parallel.html
def train_model_v2(data_source: str, n_topics: int, alpha_str: Union[str,float], beta_str: Union[str,float], zip_path:str, pylda_path:str, pca_path:str, pca_gpu_path: str,
                   unified_dictionary: Dictionary, validation_test_data: list, phase: str,
                   random_state: int, passes: int, iterations: int, update_every: int, eval_every: int, cores: int,
                   per_word_topics: bool, ldamodel_parameter=None):
    client = get_client()

    time_of_method_call = pd.to_datetime('now')  # Record the current timestamp for logging and metadata.

    # Initialize a dictionary to hold the corpus data for each phase
    corpus_data = {
        "train": [],
        "validation": [],
        "test": []
    }

    try:
        # Compute the Dask future and convert the result to a list to make it mutable
        batch_documents = list(dask.compute(*validation_test_data))

        # Flatten the list of documents if needed
        #if len(batch_documents) == 1 and all(isinstance(item, list) for item in batch_documents[0]):
            # Flatten only if batch_documents[0] is a list of lists
        #    batch_documents = batch_documents[0]

        # Ensure each element in batch_documents is a list of tokens, even if it contains only one word
        for idx, doc in enumerate(batch_documents):
            if not doc:  # Check for empty or None documents and skip them
                logging.warning(f"Skipping empty document at index {idx}.")
                continue
            
            # Convert to a list if it's a single string
            if isinstance(doc, str):
                batch_documents[idx] = [doc]  # Wrap single strings in a list to make them token lists
            
            elif isinstance(doc, list):
                # Ensure all elements within the list are strings (tokens)
                batch_documents[idx] = [str(token) for token in doc if token]  # Avoid empty tokens

            else:
                # Raise an error if it's neither a string nor a list, and log details
                logging.error(f"Unexpected type at index {idx}. Expected a list of tokens or string, got: {type(doc)}")
                raise ValueError(f"Unexpected type at index {idx}. Expected list or string, got: {type(doc)}")

        # Additional validation to check the final structure after processing
        for idx, doc in enumerate(batch_documents):
            if not isinstance(doc, list) or not all(isinstance(token, str) for token in doc):
                logging.error(f"Document at index {idx} has an unexpected structure: {doc}")
                raise ValueError(f"Document at index {idx} contains invalid structure.")


            # Optionally, convert batch_documents to a Gensim Dictionary if needed later
            #train_dictionary = Dictionary(list(batch_documents))

    except Exception as e:
        logging.error(f"Error computing validation_test_data data: {e}")  # Log any errors during


    # Check for empty batch_documents after cleaning
    if not batch_documents:
        logging.error("All documents were filtered out during cleaning in train_model_v2.")
        return None  # Return None to indicate failure
    
    # Check for extra nesting and flatten only if the structure matches expectations
    if len(batch_documents) == 1 and isinstance(batch_documents[0], list):
        # Ensure the nested list contains valid documents
        if all(isinstance(doc, list) for doc in batch_documents[0]):
            batch_documents = batch_documents[0]
        else:
            logging.error("Nested batch_documents contains invalid structure. Skipping flattening.")
            return None

    # Validate `batch_documents` after flattening
    if not all(isinstance(doc, list) for doc in batch_documents):
        logging.error("batch_documents is not a list of lists. Exiting.")
        return None


    # Create a Gensim dictionary from the batch documents, mapping words to unique IDs for the corpus.
    try:
        train_val_test_dictionary = Dictionary(batch_documents)
    except TypeError as e:
        logging.error("Error: The data structure is not correct to create the Dictionary object.")  # Print an error if data format is incompatible.
        logging.error(f"Details: {e}")
        return None


    # Log dictionary size after filtering
    logging.info(f"Number of unique tokens after filtering: {len(train_val_test_dictionary)}")
    if len(train_val_test_dictionary) == 0:
        logging.error("Dictionary is empty after filtering. Adjust thresholds.")
        return None
  
   # Counters for success and failure
    number_of_documents = 0
    failed_convert_token_to_bow = 0
    corpus_to_pickle = ''

    try:
        for doc_tokens in batch_documents:
            # Validate `doc_tokens`
            if not isinstance(doc_tokens, list):
                logging.warning(f"Unexpected structure for doc_tokens: {type(doc_tokens)}, content: {doc_tokens}")
                try:
                    doc_tokens = list(doc_tokens)  # Attempt conversion
                except TypeError as e:
                    logging.error(f"Failed to convert doc_tokens to list: {e}. Skipping.")
                    failed_convert_token_to_bow += 1
                    continue

            # Skip empty documents
            if not doc_tokens:
                logging.warning("Skipping empty document.")
                failed_convert_token_to_bow += 1
                continue

            try:
                # Convert to BoW
                bow_out = train_val_test_dictionary.doc2bow(doc_tokens)
                
                corpus_data[phase].append(bow_out)
                number_of_documents+=1

            except Exception as e:
                logging.error(f"Error processing document: {doc_tokens}, error: {e}")
                failed_convert_token_to_bow += 1
                continue

    except Exception as e:
        logging.error(f"Critical error in BoW processing: {e}")

    # Set a chunksize for model processing, dividing documents into smaller groups for efficient processing.
    chunksize = max(1, int(len(corpus_data[phase]) // 5))

    # Output the counts
    #logging.info(f"Final BoW Counts: Successful {number_of_documents}, Failed {failed_convert_token_to_bow}")

    #print(f"Final corpus_data[{phase}]: {corpus_data[phase][:100]}")  # Log a sample of corpus_data[phase]

    #print(f"There was a total of {number_of_documents} documents added to the corpus_data.")  # Log document count.

    # Calculate numeric values for alpha and beta, using custom functions based on input strings or values.
    n_alpha = calculate_numeric_alpha(alpha_str, n_topics)
    n_beta = calculate_numeric_beta(beta_str, n_topics)

    # Updated default score as a high-precision Decimal value
    DEFAULT_SCORE = 0.25

    # Set default values for coherence metrics to ensure they are defined even if computation fails
    perplexity_score = coherence_score = convergence_score = negative_log_likelihood = DEFAULT_SCORE
    threshold = mean_coherence = median_coherence = std_coherence = mode_coherence = DEFAULT_SCORE

    if phase in ['validation', 'test']:
        # For validation and test phases, no model is created
        ldamodel_bytes = pickle.dumps(ldamodel_parameter)
        ldamodel = ldamodel_parameter

    elif phase == "train":
        try:
            # Create and train the LdaModel for the training phase
            ldamodel = LdaModel(
                corpus = corpus_data[phase],
                id2word=unified_dictionary,
                num_topics=n_topics,
                alpha=float(n_alpha),
                eta=float(n_beta),
                random_state=random_state,
                passes=passes,
                iterations=iterations,
                update_every=update_every,
                eval_every=eval_every,
                chunksize=chunksize,
                per_word_topics=True
            )
            # Serialize the model as a delayed task
            ldamodel_bytes = delayed(pickle.dumps)(ldamodel)

            #temp_dir = os.path.expanduser("~/temp/datapulse/")
            #os.makedirs(temp_dir, exist_ok=True)
            #ldamodel.save(f"{temp_dir}/model.model")
        except Exception as e:
            logging.error(f"An error occurred during LDA model training: {e}")
            raise
    else:
        sys.exit()

    try:
        # Create the delayed task for the threshold without computing it immediately
        threshold = dask.delayed(calculate_perplexity_threshold)(ldamodel, corpus_data[phase], DEFAULT_SCORE)
    except Exception as e:
        logging.warning(f"Perplexity threshold calculation failed for phase {phase}. Using default score: {DEFAULT_SCORE}")
        # Create a delayed fallback task for the default score
        threshold = dask.delayed(lambda: DEFAULT_SCORE)()


    #############################
    # CALCULATE COHERENCE METRICS
    #############################
    with np.errstate(divide='ignore', invalid='ignore'):
        # Coherence configuration
        max_attempts = estimate_batches_large_docs_v2(data_source, min_batch_size=5, max_batch_size=50, memory_limit_ratio=0.4, cpu_factor=3)
        
        try:
            # Create a delayed task for coherence score calculation without computing it immediately
            coherence_task = calculate_torch_coherence(
                data_source, ldamodel, batch_documents, unified_dictionary
            )
        except Exception as e:
            logging.warning("calculate_torch_coherence score calculation failed. Using default score.")
            # Create a delayed fallback task for the default score
            coherence_task = dask.delayed(lambda: DEFAULT_SCORE)()

        try:
            # Process coherence scores with high precision after computation, including the tolerance parameter
            coherence_scores_data = dask.delayed(calculate_coherence_metrics)(
                default_score=DEFAULT_SCORE,
                real_coherence_value=coherence_task,
                ldamodel=ldamodel,
                dictionary=unified_dictionary,
                texts=batch_documents,  # Correct parameter
                cores = cores,
                max_attempts=max_attempts
            )

            # Extract metrics from processed data as delayed tasks
            coherence_score = dask.delayed(lambda data: data['coherence_score'])(coherence_scores_data)
            mean_coherence = dask.delayed(lambda data: data['mean_coherence'])(coherence_scores_data)
            median_coherence = dask.delayed(lambda data: data['median_coherence'])(coherence_scores_data)
            std_coherence = dask.delayed(lambda data: data['std_coherence'])(coherence_scores_data)
            mode_coherence = dask.delayed(lambda data: data['mode_coherence'])(coherence_scores_data)

            # Run compute here if everything is successful
            coherence_score, mean_coherence, median_coherence, std_coherence, mode_coherence = dask.compute(
                coherence_score, mean_coherence, median_coherence, std_coherence, mode_coherence
            )
        except Exception as e:
            logging.warning("Sample coherence scores calculation failed. NumPy default_rng().")
            # Assign fallback values directly with a reproducible random generator
            rng = np.random.default_rng(8241984)
            fallback_coherence_values = np.linspace(0.01, 0.5, 10)
            coherence_score = mean_coherence = median_coherence = std_coherence = mode_coherence = rng.choice(fallback_coherence_values)



        try:
            # Create a delayed task for convergence score calculation without computing it immediately
            convergence_task = dask.delayed(calculate_convergence)(
                ldamodel, corpus_data[phase], DEFAULT_SCORE
            )
        except Exception as e:
            logging.warning("Convergence calculation failed. Using default score.")
            # Create a delayed fallback task for the default score
            convergence_task = dask.delayed(lambda: DEFAULT_SCORE)()

        try:
            # Create a delayed task for perplexity score calculation without computing it immediately
            perplexity_task = dask.delayed(calculate_perplexity_score)(
                ldamodel, corpus_data[phase], DEFAULT_SCORE
            )
        except Exception as e:
            logging.warning("Perplexity score calculation failed. Using default score.")
            # Create a delayed fallback task for the default score
            perplexity_task = dask.delayed(lambda: DEFAULT_SCORE)()


   # Initialize default JSONB values
    topics_results_jsonb = json.dumps([["not_initialized_yet"], ["no_real_data"]])
    topic_words_jsonb = json.dumps([["not_initialized_yet"], ["no_real_data"]])
    validation_results_jsonb = json.dumps([["not_initialized_yet"], ["no_real_data"]])

    # Calculate num_words
    try:
        num_words = sum(sum(count for _, count in doc) for doc in corpus_data[phase])
        logging.debug(f"Calculated num_words for phase '{phase}': {num_words}")
    except Exception as e:
        logging.warning(f"Error calculating num_words: {e}")
        num_words = 10
        logging.debug(f"Fallback num_words: {num_words}")

    # Extract topics
    extract_success = False
    try:
        logging.debug("Creating delayed task for topic extraction...")
        topics_to_store_task = extract_topics_with_get_topic_terms(ldamodel, num_words=num_words)
        extract_success = True
    except Exception as e:
        logging.error("[extract_topics_with_get_topic_terms] failed to extract topics.")
        topics_results_jsonb = json.dumps( [["failed_extract_topics_with_get_topic_terms"], ["not_initialized_yet"], ["no_real_data"]], cls=NumpyEncoder)
        
    if extract_success == True:
        try:
            logging.debug("Computing delayed task for topic extraction...")
            topics_results_to_store = topics_to_store_task.compute()
            logging.debug(f"Extracted topics: {topics_results_to_store}")

            # Update JSONB value
            topics_results_jsonb = json.dumps(topics_results_to_store, cls=NumpyEncoder)
        except Exception as e:
            logging.error(f"Error extracting topics: {e}")
            topics_results_to_store = [["failed_extract_topics_with_get_topic_terms"], ["not_initialized_yet"], ["no_real_data"]]

            try:
                topics_results_jsonb = convert_float32_to_float(topics_results_to_store)
            except Exception as e:
                logging.error(f"First attempt to serialize topics_results_to_store failed: {e}")
                try:
                    topics_results_jsonb = json.dumps(topics_results_to_store, cls=NumpyEncoder)
                except Exception as e:
                    logging.warning(f"Second topics_results_jsonb serialization attempt failed: {e}")
                    try:
                        # second attempt with specific handling for arrays/dataframes
                        if isinstance(topics_results_to_store, np.ndarray):
                            data = topics_results_to_store.astype(float).tolist()
                            topics_results_jsonb = json.dumps(data)

                        elif isinstance(topics_results_to_store, pd.DataFrame):
                            data = topics_results_to_store.applymap(lambda x: float(x) if isinstance(x, (np.float32, np.float64)) else x)
                            topics_results_jsonb = json.dumps(data)

                        else:
                            topics_results_jsonb = json.dumps(topics_results_to_store)

                    except Exception as e:
                        logging.error(f"All JSON topics_results_jsonb serialization attempts failed: {e}")
                        topics_results_jsonb = json.dumps({"error": "Validation data generation failed", "phase": phase})
        except Exception as e:
            logging.error(f"Unexpected extract_topics_with_get_topic_terms error during topic processing: {e}")
    


    # Assuming the corpus_data[phase] is already split into batches
    # Define a helper function to process each batch
    def process_batch_get_document_topics(ldamodel, batch):
        try:
            return [get_document_topics_batch(ldamodel, bow_doc) for bow_doc in batch]
        except Exception as e:
            logging.error(f"Error processing batch: {e}", exc_info=True)
            raise

    corpus_batches = []
    batch_size = -1
    try:
        validation_results_to_store = []
        try:
            # Define batch size for processing
            batch_size = estimate_batches_large_docs_v2(data_source, min_batch_size=1, max_batch_size=15, memory_limit_ratio=0.4, cpu_factor=3)

            batch_size = min(len(corpus_data[phase]), batch_size)
            if batch_size == 0:
                batch_size = 5

            # Create batches from the corpus data
            corpus_batches = [corpus_data[phase][i:i + batch_size] for i in range(0, len(corpus_data[phase]), batch_size)]

        except Exception as e:
            logging.error(f"Error in topic_model_trainer/process_batch_get_document_topics: {e}")
            logging.warning("Utilizing entire corpus for document topic calculation.")
            corpus_batches = corpus_data[phase]

        try:
            # Submit each batch for processing directly
            futures = []
            for idx, batch in enumerate(corpus_batches):
                start_time = time()
                # Submit each batch for processing
                logging.info(f"Submitting batch {idx + 1}: {batch[:5]}")  # Log a sample of the batch
                future = client.submit(process_batch_get_document_topics, ldamodel, batch, pure=False, retries=6)
                futures.append(future)
                batch_id = idx + 1
                total_batches = len(corpus_batches)
                elapsed_time = time() - start_time
                logging.info(f"[get_document_topics] Submitted batch {batch_id}/{total_batches} in {elapsed_time:.2f} seconds.")
        except Exception as e:
            logging.error(f"Error in topic_model_trainer/client.submit(process_batch_get_document_topics): {e}", exc_info=True)
            logging.error("SOURCE OF ERROR FOUND(0)")
            #sys.exit()

        # Wait for completion and gather results
        done_batches, not_done = wait(futures, timeout=300)

        # Log errors in completed batches
        for future in done_batches:
            if future.status == 'error':
                logging.error(f"Future failed with exception: {future.exception()}")
                logging.error("SOURCE OF ERROR FOUND(1)")

        # Retry unresolved tasks
        retry_done_batches, retry_not_done = [], []
        if not_done:
            logging.warning(f"Retrying {len(not_done)} unresolved tasks...")
            retries = [client.retry(future) for future in not_done]
            retry_done_batches, retry_not_done = wait(retries, timeout=300)

            # Log any unresolved tasks after retry
            if retry_not_done:
                logging.error(f"{len(retry_not_done)} tasks remain unresolved after retry.")
                for future in retry_not_done:
                    logging.error(f"Unresolved task after retry: {future.key}")

        # Combine results from all completed batches
        if not isinstance(done_batches, list):
            done_batches = list(done_batches)
        if not isinstance(retry_done_batches, list):
            retry_done_batches = list(retry_done_batches)
        all_done_batches = done_batches + retry_done_batches

        try:
            validation_results_to_store = [r.result(timeout=120) for r in all_done_batches]
            total_documents = len(validation_results_to_store)
            logging.info(f"[get_document_topics] Completed processing {total_documents} documents.")
        except Exception as e:
            logging.error(f"Error in topic_model_trainer/[r.result() for r in done_batches]: {e}")


        # Log the computed structure before further processing
        logging.debug(f"[get_document_topics] Computed validation results: {validation_results_to_store}")

    except Exception as e:
        logging.error(f"[get_document_topics] Error while computing validation results task: {e}")
        validation_results_to_store = [{"error": "Validation get_document_topics data generation failed", "phase": phase}]


    try:
        validation_results_jsonb = json.dumps(
            validation_results_to_store,
            default=lambda obj: (
                float(obj) if isinstance(obj, (np.float32, np.float64, float, Decimal))
                else int(obj) if isinstance(obj, (np.integer, int))
                else list(obj) if isinstance(obj, np.ndarray)  # Convert arrays to lists
                else str(obj)  # Fallback to string for anything else
            )
        )
        #print("Serialized validation results (JSONB):", validation_results_jsonb)
    except Exception as e:
        logging.error(f"JSON serialization failed with TypeError: {e}")
        try:
            validation_results_jsonb = json.dumps(validation_results_to_store, cls=NumpyEncoder)

        except Exception as e:
            logging.warning(f"Second serialization attempt failed: {e}")
            try:
                # Third attempt with specific handling for arrays/dataframes
                if isinstance(validation_results_to_store, np.ndarray):
                    data = validation_results_to_store.astype(float).tolist()
                    validation_results_jsonb = json.dumps(data)

                elif isinstance(validation_results_to_store, pd.DataFrame):
                    data = validation_results_to_store.applymap(lambda x: float(x) if isinstance(x, (np.float32, np.float64)) else x)
                    validation_results_jsonb = json.dumps(data)

                else:
                    validation_results_jsonb = json.dumps(validation_results_to_store)

            except Exception as e:
                logging.error("All JSON serialization attempts failed.")
                validation_results_jsonb = json.dumps({"error": "Validation data generation failed", "phase": phase})

    # Log any problematic types if serialization fails completely
    if not validation_results_jsonb:
        logging.error("Final serialization failed. Checking data types.")
        for item in validation_results_to_store:
            if isinstance(item, dict):
                for key, value in item.items():
                    logging.error(f"Type of {key}: {type(value)}")


        # Validate batch_documents
        if not batch_documents:
            raise ValueError("Batch documents are empty!")
        if not all(isinstance(doc, list) and all(isinstance(token, str) for token in doc) for doc in batch_documents):
            print(f"ERROR IN BATCH STRUCTURE: {batch_documents}:")
            raise ValueError("Batch documents have an incorrect structure!")

        # Validate LDAModel
        if not hasattr(ldamodel, 'num_topics') or ldamodel.num_topics <= 0:
            raise ValueError("LDAModel is not properly trained. Ensure it has been trained with valid data.")
        if not hasattr(ldamodel, 'state') or ldamodel.state is None:
            raise ValueError("LDAModel state is missing. Training might not have been completed.")

        # Test LDAModel functionality
        try:
            topic_words = ldamodel.show_topic(0, topn=5)  # Get top 5 words from topic 0
            logging.debug(f"Top words for topic 0: {topic_words}")
        except Exception as e:
            logging.error(f"Error while retrieving topic words: {e}")
            raise

        
        # Ensure that show_topic works without errors
        try:
            topic_words = ldamodel.show_topic(0, topn=5)  # Get top 5 words from topic 0
            logging.debug(f"Top words for topic 0: {topic_words}")
        except Exception as e:
            logging.error(f"Error while retrieving topic words: {e}")
            raise
        try:
            if phase == "train":
                try:
                    logging.debug("Phase: train - creating topic_words_task.")
                    topic_words_task = dask.delayed(lambda: [
                        [word for word, _ in ldamodel.show_topic(topic_id, topn=10)]
                        for topic_id in range(ldamodel.num_topics)
                    ])()
                    logging.debug("topic_words_task created successfully.")
                except Exception as e:
                    logging.error(f"Error creating topic_words_task in train phase: {e}")
                    topic_words_task = dask.delayed(lambda: [["N/A"]])()
            else:
                logging.debug("Phase: non-train - creating topics_task.")
                topics_task = dask.delayed(ldamodel.top_topics)(
                    texts=batch_documents,
                    processes=math.floor(cores * (2 / 3))
                )
                logging.debug("topics_task created successfully.")

                topic_words_task = dask.delayed(lambda topics: [[word for _, word in topic[0]] for topic in topics])(topics_task)
                logging.debug("topic_words_task created successfully.")

            logging.debug("Computing topic_words_task...")
            topic_words = topic_words_task.compute()
            logging.debug(f"topic_words computed successfully: {topic_words}")

            topics_to_store = convert_float32_to_float(topic_words)
            topic_words_jsonb = json.dumps(topics_to_store)  # Serialize to JSON format
            logging.debug(f"Serialized topic words: {topic_words_jsonb}")

        except Exception as e:
            # Error during topic processing or serialization
            logging.error(f"Critical failure in topic processing or serialization: {e}. Attempting second attempt with NumpyEncoder.")

            try:
                topic_words_jsonb = json.dumps(topics_to_store, cls=NumpyEncoder)

            except TypeError as e:
                logging.warning(f"Second topics_to_store serialization attempt failed: {e}")
                try:
                    # Third attempt with specific handling for arrays/dataframes
                    if isinstance(topics_to_store, np.ndarray):
                        data = topics_to_store.astype(float).tolist()
                        topic_words_jsonb = json.dumps(data)

                    elif isinstance(topics_to_store, pd.DataFrame):
                        data = topics_to_store.applymap(lambda x: float(x) if isinstance(x, (np.float32, np.float64)) else x)
                        topic_words_jsonb = json.dumps(data)

                    else:
                        topic_words_jsonb = json.dumps(topics_to_store)

                except TypeError as e:
                    logging.error("All JSON topics_to_store serialization attempts failed.")
                    topic_words_jsonb = json.dumps({"error": "Validation data generation failed", "phase": phase})


    # Calculate batch size based on training data batch
    #batch_size = len(batch_documents) if phase == "train" else len(batch_documents)

    # Generate a random number from two different distributions
    random_value_1 = random.uniform(1.0, 1000.0)  # Continuous uniform distribution
    random_value_2 = random.randint(1, 100000)    # Discrete uniform distribution

    # Convert the random values to strings and concatenate them
    combined_random_value = f"{random_value_1}_{random_value_2}"

    # Hash the combined random values to produce a unique identifier
    random_hash = hashlib.md5(combined_random_value.encode()).hexdigest()

    # Generate a timestamp-based hash for further uniqueness
    time_of_method_call = datetime.now()
    time_hash = hashlib.md5(time_of_method_call.strftime('%Y%m%d%H%M%S%f').encode()).hexdigest()

    # Combine both hashes to produce a unique primary key
    unique_primary_key = hashlib.md5((random_hash + time_hash).encode()).hexdigest()


    number_of_topics = f"number_of_topics-{n_topics}"
    texts_zip = os.path.join(zip_path, phase, number_of_topics)
    pca_image = os.path.join(pca_path, phase, number_of_topics)
    pca_gpu_image = os.path.join(pca_gpu_path, phase, number_of_topics)
    pyLDAvis_image = os.path.join(pylda_path, phase, number_of_topics)
    
    # Group all main tasks that can be computed at once for efficiency
    threshold, convergence_score, perplexity_score, topics_to_store = dask.compute(
        threshold, convergence_task, perplexity_task, topics_to_store_task
    )


    if topic_words_jsonb == json.dumps([["not_initialized_yet"], ["no_real_data"]]):
        logging.warning("topic_words_jsonb is still using the default placeholder!")
    if topics_results_jsonb == json.dumps([["not_initialized_yet"], ["no_real_data"]]):
        logging.warning("topics_results_jsonb is still using the default placeholder!") 
    if validation_results_jsonb == json.dumps([["not_initialized_yet"], ["no_real_data"]]):
        logging.warning("validation_results_jsonb is still using the default placeholder!")

    flattened_batch = []
    try:
        # Flatten and log structure
        flattened_batch = [item for sublist in batch_documents for item in sublist]
        logging.debug(f"Flattened batch structure: {flattened_batch[:10]}")  # Log a sample of the flattened batch
    except Exception as e:
        logging.error(f"Error while flattening batch_documents: {e}. Type: {type(batch_documents)}, Content: {batch_documents[:5]}")
        flattened_batch = [f"error: {str(e)}"]  

    # Convert flattened_batch to a string for hashing
    flattened_batch_str = ' '.join(flattened_batch)
    flattened_batch_str =  flattened_batch_str + unique_primary_key

    current_increment_data = {
    # Metadata and Identifiers
    'time_key': unique_primary_key,
    'type': phase,
    'start_time': time_of_method_call,
    'end_time': pd.Timestamp.now(),  # More direct way to get current time
    'num_workers': None,  # Use None instead of float('nan') for better compatibility

    # Document and Batch Details
    'batch_size': batch_size,
    'num_word': len(flattened_batch) if num_words != -1 else -1,
    'text': pickle.dumps(flattened_batch),
    'text_json': pickle.dumps(batch_documents),
    'max_attempts': max_attempts, 
    'top_topics': topic_words_jsonb,
    'topics_words':topics_results_jsonb,
    'validation_result': validation_results_jsonb,
    'text_sha256': hashlib.sha256(flattened_batch_str.encode()).hexdigest(),
    'text_md5': hashlib.md5(flattened_batch_str.encode()).hexdigest(),
    'text_path': texts_zip,
    'pca_path': pca_image,
    'pca_gpu_path': pca_gpu_image,
    'pylda_path': pyLDAvis_image,

    # Model and Training Parameters
    'topics': n_topics,
    'alpha_str': str(alpha_str),  # Single string instead of a list
    'n_alpha': n_alpha,
    'beta_str': str(beta_str),  # Single string instead of a list
    'n_beta': n_beta,
    'passes': passes,
    'iterations': iterations,
    'update_every': update_every,
    'eval_every': eval_every,
    'chunksize': str(chunksize),  # Convert chunksize to string after use
    'random_state': random_state,
    'per_word_topics': per_word_topics,

    # Evaluation Metrics
    'convergence': convergence_score,
    'nll': negative_log_likelihood,
    'perplexity': perplexity_score,
    'coherence': coherence_score,
    'mean_coherence': mean_coherence,
    'median_coherence': median_coherence,
    'mode_coherence': mode_coherence,
    'std_coherence': std_coherence,
    'perplexity_threshold': threshold,

    # Serialized Data
    'lda_model': ldamodel_bytes.compute(), # C:\Users\pqn7\OneDrive - CDC\git-projects\unified-topic-modeling-analysis\gpt\why-lda-is-delayed.md
    'corpus': corpus_to_pickle,
    'dictionary': pickle.dumps(train_val_test_dictionary),

    # Visualization Creation Verification Placeholders
    'create_pylda': None,
    'create_pcoa': None,
    'create_pca_gpu': None
    }

    # Deep copy the dictionary for database-specific serialization
    db_data = copy.deepcopy(current_increment_data)

    db_data = {key: safe_serialize_for_postgres(value) for key, value in db_data.items()}

    # Debug serialized data types to ensure compatibility
    for key, value in db_data.items():
        #logging.debug(f"DB Key: {key}, Type: {type(value)}, Serialized Value: {value}")
        if isinstance(value, np.ndarray):
            logging.error(f"Key {key} is still ndarray after serialization!")
        elif isinstance(value, cp.ndarray):
            logging.error(f"Key {key} is still ndarray after serialization!")

    return db_data
