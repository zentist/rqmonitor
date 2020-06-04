from flask import (
    current_app,
    render_template,
    request,
    jsonify
)
from six import string_types
from flask import Blueprint
from rqmonitor.utils import (
                            list_all_queues_names, list_all_possible_job_status,
                            list_all_queues, reformat_job_data, delete_workers,
                            create_redis_connection, delete_queue, empty_queue,
                            delete_job, cancel_job, requeue_job, get_redis_memory_used,
                            job_count_in_queue_registry, resolve_jobs,
                            delete_all_jobs_in_queues_registries, requeue_all_jobs_in_failed_registry,
                            cancel_all_queued_jobs
)

from rq.connections import pop_connection, push_connection
from rqmonitor.decorators import cache_control_no_store
from rqmonitor.exceptions import RQMonitorException, ActionFailed
from rq.worker import Worker
import logging


logger = logging.getLogger(__name__)
stream_handler = logging.StreamHandler()
logger.addHandler(stream_handler)
logger.setLevel(logging.INFO)

REDIS_RQ_HOST = 'localhost:6379'

monitor_blueprint = Blueprint('rqmonitor', __name__, template_folder='templates', static_folder='static')


# Plan to separate between HTTP and non HTTP errors by using HTTPException class
@monitor_blueprint.errorhandler(RQMonitorException)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


@monitor_blueprint.before_app_first_request
def setup_redis_connection():
    redis_url = current_app.config.get("RQ_MONITOR_REDIS_URL")
    print(redis_url)
    if isinstance(redis_url, string_types):
        # update as tuple
        current_app.config["RQ_MONITOR_REDIS_URL"] = (redis_url,)
        current_app.redis_connection = create_redis_connection((redis_url,)[0])
    elif isinstance(redis_url, (tuple, list)):
        current_app.redis_connection = create_redis_connection(redis_url[0])
    else:
        raise RuntimeError("No Redis configuration!")


@monitor_blueprint.before_request
def push_rq_connection():
    new_instance_number = request.view_args.get("instance_number")
    if new_instance_number is not None:
        redis_url = current_app.config.get("RQ_MONITOR_REDIS_URL")
        if new_instance_number < len(redis_url):
            new_instance = create_redis_connection(redis_url[new_instance_number])
        else:
            raise LookupError("Index exceeds RQ list. Not Permitted.")
    else:
        new_instance = current_app.redis_connection
    push_connection(new_instance)
    current_app.redis_connection = new_instance


@monitor_blueprint.teardown_request
def pop_rq_connection(exception=None):
    pop_connection()


@monitor_blueprint.route('/', defaults={"instance_number": 0})
@cache_control_no_store
def home(instance_number):
    rq_queues_list = list_all_queues_names()
    rq_possible_job_status = list_all_possible_job_status()

    return render_template('rqmonitor/index.html',
                            rq_host_url= REDIS_RQ_HOST,
                            rq_queues_list= rq_queues_list,
                            rq_possible_job_status= rq_possible_job_status,
                            redis_instance_list=current_app.config.get('RQ_MONITOR_REDIS_URL'),
                            redis_memory_used=get_redis_memory_used()
                           )


@monitor_blueprint.route('/jobs_dashboard')
@cache_control_no_store
def get_jobs_dashboard():
    return render_template('rqmonitor/jobs.html')


@monitor_blueprint.route('/workers_dashboard')
@cache_control_no_store
def get_workers_dashboard():
    return render_template('rqmonitor/workers.html')


@monitor_blueprint.route('/queues_dashboard')
@cache_control_no_store
def get_queues_dashboard():
    return render_template('rqmonitor/queues.html')


@monitor_blueprint.route('/queues', methods=['GET'])
@cache_control_no_store
def list_queues_api():
    queue_list = list_all_queues()
    rq_queues = []
    for queue in queue_list:
        rq_queues.append(
            {
                'queue_name': queue.name,
                'job_count': queue.count,
            }
        )

    return {
        'rq_host_url': REDIS_RQ_HOST,
        'rq_workers_count': len(rq_queues),
        'data': rq_queues,
    }


@monitor_blueprint.route('/workers', methods=['GET'])
@cache_control_no_store
def list_workers_api():
    workers_list = Worker.all()
    rq_workers = []
    for worker in workers_list:
        rq_workers.append(
            {
                'worker_name': worker.name,
                'listening_on': ', '.join(queue.name for queue in worker.queues),
                'status': worker.get_state(),
                'current_job_id': worker.get_current_job_id(),
                'success_jobs': worker.successful_job_count,
                'failed_jobs': worker.failed_job_count,
            }
        )

    return {
        'rq_workers_count': len(rq_workers),
        'data': rq_workers,
    }


@monitor_blueprint.route('/jobs', methods=['GET'])
@cache_control_no_store
def list_jobs_api():
    """
    :param request: Flask GET request containing two parameters acting as filter for jobs
                    1) Jobs Status list (with these status)
                    2) queues list (to fetch queues)
    :return: rendered output
    """
    serialised_jobs = []

    start = int(request.args.get('start'))
    length = int(request.args.get('length'))
    draw = int(request.args.get('draw'))
    search = request.args.get('search[value]')

    request_source = request.args.get('from_datatable', None)

    requested_queues = request.args.getlist('queues[]')
    if requested_queues is None:
        requested_queues = list_all_queues_names()
    requested_job_status = request.args.getlist('jobstatus[]')
    if requested_job_status is None:
        requested_job_status = list_all_possible_job_status()

    if not requested_queues or not requested_job_status:
        return {
            'data': serialised_jobs,
        }

    job_counts = []
    total_job_count = 0

    for queue in requested_queues:
        for job_status in requested_job_status:
            queue_registry_count = job_count_in_queue_registry(queue, job_status)
            job_counts.append((queue, job_status, queue_registry_count))
            total_job_count += queue_registry_count

    jobs = resolve_jobs(job_counts, start, length)

    for job in jobs:
        serialised_jobs.append(reformat_job_data(job))

    return {
        'draw': draw,
        'recordsTotal': total_job_count,
        'recordsFiltered': total_job_count,
        'data': serialised_jobs,
    }


@monitor_blueprint.route('/workers/delete', methods=['POST'])
@cache_control_no_store
def delete_workers_api():
    worker_names = []
    if request.method == 'POST':
        worker_id = request.form.get('worker_id', None)
        delete_all = request.form.get('delete_all')
        if worker_id is None and delete_all is "false":
            raise RQMonitorException('Worker ID not received', status_code=400)
        try:
            if delete_all == "true":
                for worker_instance in Worker.all():
                    worker_names.append(worker_instance.name)
                delete_workers(worker_names)
            else:
                worker_names.append(worker_id)
                delete_workers([worker_id])
        except ActionFailed:
            raise RQMonitorException(f'Unable to delete worker/s', status_code=500)

        return {
            'message': f'Successfully deleted worker {worker_names}'
        }

    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/queues/delete', methods=['POST'])
@cache_control_no_store
def delete_queue_api():
    if request.method == 'POST':
        queue_id = request.form.get('queue_id', None)
        if queue_id is None:
            raise RQMonitorException('Queue Name not received', status_code=400)
        try:
            delete_queue(queue_id)
        except ActionFailed as e:
            raise RQMonitorException(f'Unable to delete Queue {queue_id}', status_code=500)
        return {
            'message': f'Successfully deleted {queue_id}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)



@monitor_blueprint.route('/queues/empty', methods=['POST'])
@cache_control_no_store
def empty_queue_api():
    if request.method == 'POST':
        queue_id = request.form.get('queue_id', None)
        if queue_id is None:
            raise RQMonitorException('Queue Name not received', status_code=400)
        try:
            empty_queue(queue_id)
        except ActionFailed as e:
            raise RQMonitorException(f'Unable to empty Queue {queue_id}', status_code=500)
        return {
            'message': f'Successfully emptied {queue_id}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/queues/delete/all', methods=['POST'])
@cache_control_no_store
def delete_all_queues_api():
    if request.method == 'POST':
        queue_names = [queue.name for queue in list_all_queues()]
        for queue in list_all_queues():
            queue.delete(delete_jobs=True)
        return {
            'message': f'Successfully deleted queues {queue_names}'
        }
    else:
        raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/queues/empty/all', methods=['POST'])
@cache_control_no_store
def empty_all_queues_api():
    if request.method == 'POST':
        queue_names = [queue.name for queue in list_all_queues()]
        for queue in list_all_queues():
            queue.empty()
        return {
            'message': f'Successfully emptied queues {queue_names}'
        }
    else:
        raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/workers/info', methods=['GET'])
@cache_control_no_store
def worker_info_api():
    if request.method == 'GET':
        worker_id = request.args.get('worker_id', None)

        if worker_id is None:
            raise RQMonitorException('Worker ID not received !', status_code=400)

        worker_instance = Worker.find_by_key(Worker.redis_worker_namespace_prefix + worker_id)
        return {
            'worker_host_name': worker_instance.hostname.decode('utf-8'),
            'worker_ttl': worker_instance.default_worker_ttl,
            'worker_result_ttl': worker_instance.default_result_ttl,
            'worker_name': worker_instance.name,
            'worker_birth_date': worker_instance.birth_date.strftime('%d-%m-%Y %H:%M:%S')
                                if worker_instance.birth_date is not None else "Not Available",
            'worker_death_date': worker_instance.death_date.strftime('%d-%m-%Y %H:%M:%S')
                                if worker_instance.death_date is not None else "Is Alive",
            'worker_last_cleaned_at': worker_instance.last_cleaned_at.strftime('%d-%m-%Y %H:%M:%S')
                                if worker_instance.last_cleaned_at is not None else "Not Yet Cleaned",
            'worker_failed_job_count': worker_instance.failed_job_count,
            'worker_successful_job_count': worker_instance.successful_job_count,
            'worker_job_monitoring_interval': worker_instance.job_monitoring_interval,
            'worker_last_heartbeat': worker_instance.last_heartbeat.strftime('%d-%m-%Y %H:%M:%S')
                                if worker_instance.last_heartbeat is not None else "Not Available",
            'worker_current_job_id': worker_instance.get_current_job_id(),
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/jobs/cancel', methods=['POST'])
@cache_control_no_store
def cancel_job_api():
    if request.method == 'POST':
        job_id = request.form.get('job_id')
        if job_id is None:
            raise RQMonitorException('Job ID not received', status_code=400)
        try:
            cancel_job(job_id)
        except ActionFailed:
            raise RQMonitorException(f'Unable to cancel {job_id}', status_code=500)
        return {
            'message': f'Successfully cancelled job with ID {job_id}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/jobs/requeue',  methods=['POST'])
@cache_control_no_store
def requeue_job_api():
    if request.method == 'POST':
        job_id = request.form.get('job_id')
        if job_id is None:
            raise RQMonitorException('Job ID not received', status_code=400)
        try:
            requeue_job(job_id)
        except ActionFailed:
            raise RQMonitorException(f'Unable to requeue {job_id}', status_code=500)

        return {
            'message': f'Successfully requeued job with ID {job_id}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/jobs/delete',  methods=['POST'])
@cache_control_no_store
def delete_job_api():
    if request.method == 'POST':
        job_id = request.form.get('job_id')
        if job_id is None:
            raise RQMonitorException('Job ID not received', status_code=400)
        try:
            delete_job(job_id)
        except ActionFailed:
            raise RQMonitorException(f'Unable to delete {job_id}', status_code=500)

        return {
            'message': f'Successfully deleted job with ID {job_id}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/jobs/delete/all',  methods=['POST'])
@cache_control_no_store
def delete_all_jobs_api():
    if request.method == 'POST':
        requested_queues = request.form.getlist('queues[]')
        requested_job_status = request.form.getlist('jobstatus[]')
        print(requested_queues, requested_job_status)

        if requested_queues is None or requested_job_status is None:
            raise RQMonitorException('No queue/status selected', status_code=400)

        try:
            delete_all_jobs_in_queues_registries(requested_queues, requested_job_status)
        except ActionFailed:
            raise RQMonitorException(f'Unable to delete all jobs', status_code=500)

        return {
            'message': f'Successfully deleted all jobs with'
                       f' status as {requested_job_status} on queues {requested_queues}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/jobs/requeue/all',  methods=['POST'])
@cache_control_no_store
def requeue_failed_jobs_api():
    if request.method == 'POST':
        requested_queues = request.form.getlist('queues[]')
        if requested_queues is None:
            raise RQMonitorException('No queue/s selected', status_code=400)
        fail_count = 0
        try:
            fail_count = requeue_all_jobs_in_failed_registry(requested_queues)
        except ActionFailed:
            raise RQMonitorException(f'Unable to requeue all, remaining {fail_count} ', status_code=500)

        return {
            'message': f'Successfully requeued all jobs on queues {requested_queues}'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/jobs/cancel/all',  methods=['POST'])
@cache_control_no_store
def cancel_queued_jobs_api():
    if request.method == 'POST':
        requested_queues = request.form.getlist('queues[]')
        if requested_queues is None:
            raise RQMonitorException('No queue/s selected', status_code=400)
        try:
            fail_count = cancel_all_queued_jobs(requested_queues)
        except ActionFailed:
            raise RQMonitorException(f'Unable to cancel all jobs', status_code=500)

        return {
            'message': 'Successfully requeued all jobs'
        }
    raise RQMonitorException('Invalid HTTP Request type', status_code=400)


@monitor_blueprint.route('/redis/memory')
@cache_control_no_store
def redis_memory_api():
    return {
        'redis_memory_used': get_redis_memory_used()
    }