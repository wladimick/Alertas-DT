<?php
defined( 'ABSPATH' ) || exit;

class ADT_REST {

    const NAMESPACE = 'alertas-dt/v1';

    public static function register(): void {
        add_action( 'rest_api_init', [ __CLASS__, 'register_routes' ] );
    }

    public static function register_routes(): void {
        register_rest_route( self::NAMESPACE, '/health', [
            'methods'             => WP_REST_Server::READABLE,
            'callback'            => [ __CLASS__, 'health' ],
            'permission_callback' => '__return_true',
        ] );

        register_rest_route( self::NAMESPACE, '/subscribers', [
            'methods'             => WP_REST_Server::READABLE,
            'callback'            => [ __CLASS__, 'list_subscribers' ],
            'permission_callback' => [ __CLASS__, 'check_token' ],
            'args'                => [
                'status'        => [ 'type' => 'string',  'default' => 'active', 'sanitize_callback' => 'sanitize_text_field' ],
                'updated_after' => [ 'type' => 'string',  'default' => '',       'sanitize_callback' => 'sanitize_text_field' ],
                'limit'         => [ 'type' => 'integer', 'default' => 100,      'minimum' => 1, 'maximum' => 500 ],
                'page'          => [ 'type' => 'integer', 'default' => 1,        'minimum' => 1 ],
            ],
        ] );

        register_rest_route( self::NAMESPACE, '/subscribers/synced', [
            'methods'             => WP_REST_Server::CREATABLE,
            'callback'            => [ __CLASS__, 'mark_synced' ],
            'permission_callback' => [ __CLASS__, 'check_token' ],
        ] );
    }

    public static function check_token( WP_REST_Request $request ): bool {
        $token = ADT_Settings::token_from_request( $request );
        return ADT_Settings::verify_token( $token );
    }

    public static function health(): WP_REST_Response {
        return new WP_REST_Response( [
            'ok'      => true,
            'plugin'  => 'alertas-dt-bridge',
            'version' => ADT_VERSION,
        ], 200 );
    }

    public static function list_subscribers( WP_REST_Request $request ): WP_REST_Response {
        $args = [
            'status'        => $request->get_param( 'status' ),
            'updated_after' => $request->get_param( 'updated_after' ),
            'limit'         => (int) $request->get_param( 'limit' ),
            'page'          => (int) $request->get_param( 'page' ),
        ];

        $rows  = ADT_Database::list( $args );
        $total = ADT_Database::count( $args['status'] );

        $subscribers = array_map( function ( array $row ): array {
            return [
                'id'               => (int) $row['id'],
                'email'            => $row['email'],
                'status'           => $row['status'],
                'consent'          => (bool) $row['consent'],
                'consent_at'       => $row['consent_at'] ?? null,
                'source_page'      => $row['source_page'] ?? null,
                'source_url'       => $row['source_url']  ?? null,
                'created_at'       => $row['created_at'],
                'updated_at'       => $row['updated_at'],
                'synced_at'        => $row['synced_at'] ?? null,
                'subscriber_name'  => $row['subscriber_name'] ?? null,
                'phone'            => $row['phone'] ?? null,
                'whatsapp_consent' => (bool) ( $row['whatsapp_consent'] ?? false ),
            ];
        }, $rows );

        return new WP_REST_Response( [
            'ok'          => true,
            'total'       => $total,
            'page'        => $args['page'],
            'limit'       => $args['limit'],
            'subscribers' => $subscribers,
        ], 200 );
    }

    public static function mark_synced( WP_REST_Request $request ): WP_REST_Response {
        $body = $request->get_json_params();

        $ids = array_filter(
            array_map( 'intval', (array) ( $body['ids'] ?? [] ) )
        );
        $synced_at = sanitize_text_field( $body['synced_at'] ?? current_time( 'mysql', true ) );

        if ( empty( $ids ) ) {
            return new WP_REST_Response( [ 'ok' => false, 'error' => 'ids requerido.' ], 400 );
        }

        $updated = ADT_Database::mark_synced( array_values( $ids ), $synced_at );
        ADT_Settings::set_last_sync( $synced_at );

        return new WP_REST_Response( [ 'ok' => true, 'updated' => $updated ], 200 );
    }
}
