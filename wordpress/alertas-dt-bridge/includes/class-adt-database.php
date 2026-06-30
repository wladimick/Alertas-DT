<?php
defined( 'ABSPATH' ) || exit;

class ADT_Database {

    public static function get_table(): string {
        global $wpdb;
        return $wpdb->prefix . ADT_TABLE;
    }

    public static function create_table(): void {
        global $wpdb;
        $table      = self::get_table();
        $charset    = $wpdb->get_charset_collate();

        $sql = "CREATE TABLE IF NOT EXISTS {$table} (
            id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            email            VARCHAR(190)    NOT NULL,
            status           VARCHAR(30)     NOT NULL DEFAULT 'active',
            consent          TINYINT(1)      NOT NULL DEFAULT 0,
            consent_at       DATETIME        NULL,
            source_page      TEXT            NULL,
            source_url       TEXT            NULL,
            ip_hash          VARCHAR(128)    NULL,
            user_agent       TEXT            NULL,
            created_at       DATETIME        NOT NULL,
            updated_at       DATETIME        NOT NULL,
            synced_at        DATETIME        NULL,
            last_error       TEXT            NULL,
            subscriber_name  VARCHAR(255)    NULL,
            phone            VARCHAR(30)     NULL,
            whatsapp_consent TINYINT(1)      NOT NULL DEFAULT 0,
            PRIMARY KEY (id),
            UNIQUE KEY email_unique (email)
        ) {$charset};";

        require_once ABSPATH . 'wp-admin/includes/upgrade.php';
        dbDelta( $sql );
    }

    public static function drop_table(): void {
        global $wpdb;
        $table = self::get_table();
        // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
        $wpdb->query( "DROP TABLE IF EXISTS {$table}" );
    }

    /**
     * Upsert subscriber. Returns [ 'id' => int, 'created' => bool ].
     */
    public static function upsert( array $data ): array {
        global $wpdb;
        $table = self::get_table();
        $now   = current_time( 'mysql', true ); // UTC

        $email = sanitize_email( strtolower( trim( $data['email'] ?? '' ) ) );
        if ( ! is_email( $email ) ) {
            throw new InvalidArgumentException( 'Ingresa un correo electrónico válido.' );
        }
        if ( empty( $data['consent'] ) ) {
            throw new InvalidArgumentException( 'Debes aceptar recibir alertas para continuar.' );
        }

        $existing = $wpdb->get_row(
            $wpdb->prepare( "SELECT id FROM {$table} WHERE email = %s", $email ), // phpcs:ignore
            ARRAY_A
        );

        $subscriber_name  = isset( $data['subscriber_name'] ) ? sanitize_text_field( $data['subscriber_name'] ) : null;
        $phone            = isset( $data['phone'] )           ? sanitize_text_field( $data['phone'] )           : null;
        $whatsapp_consent = ! empty( $data['whatsapp_consent'] ) ? 1 : 0;

        if ( $existing ) {
            $wpdb->update(
                $table,
                [
                    'status'           => 'active',
                    'consent'          => 1,
                    'consent_at'       => $now,
                    'source_page'      => $data['source_page'] ?? null,
                    'source_url'       => $data['source_url']  ?? null,
                    'updated_at'       => $now,
                    'subscriber_name'  => $subscriber_name,
                    'phone'            => $phone,
                    'whatsapp_consent' => $whatsapp_consent,
                ],
                [ 'id' => $existing['id'] ],
                [ '%s', '%d', '%s', '%s', '%s', '%s', '%s', '%s', '%d' ],
                [ '%d' ]
            );
            return [ 'id' => (int) $existing['id'], 'created' => false ];
        }

        $ip_hash = ! empty( $data['ip'] ) ? hash( 'sha256', $data['ip'] ) : null;

        $wpdb->insert(
            $table,
            [
                'email'            => $email,
                'status'           => 'active',
                'consent'          => 1,
                'consent_at'       => $now,
                'source_page'      => $data['source_page'] ?? null,
                'source_url'       => $data['source_url']  ?? null,
                'ip_hash'          => $ip_hash,
                'user_agent'       => isset( $data['user_agent'] ) ? substr( $data['user_agent'], 0, 512 ) : null,
                'created_at'       => $now,
                'updated_at'       => $now,
                'subscriber_name'  => $subscriber_name,
                'phone'            => $phone,
                'whatsapp_consent' => $whatsapp_consent,
            ],
            [ '%s', '%s', '%d', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%d' ]
        );
        return [ 'id' => (int) $wpdb->insert_id, 'created' => true ];
    }

    public static function count( string $status = '' ): int {
        global $wpdb;
        $table = self::get_table();
        if ( $status ) {
            return (int) $wpdb->get_var(
                $wpdb->prepare( "SELECT COUNT(*) FROM {$table} WHERE status = %s", $status ) // phpcs:ignore
            );
        }
        return (int) $wpdb->get_var( "SELECT COUNT(*) FROM {$table}" ); // phpcs:ignore
    }

    public static function list( array $args = [] ): array {
        global $wpdb;
        $table = self::get_table();

        $where  = [];
        $params = [];

        if ( ! empty( $args['status'] ) ) {
            $where[]  = 'status = %s';
            $params[] = $args['status'];
        }
        if ( ! empty( $args['updated_after'] ) ) {
            $where[]  = 'updated_at >= %s';
            $params[] = $args['updated_after'];
        }

        $limit  = min( (int) ( $args['limit'] ?? 100 ), 500 );
        $offset = ( max( 1, (int) ( $args['page'] ?? 1 ) ) - 1 ) * $limit;

        $sql = "SELECT id, email, status, consent, consent_at, source_page, source_url, created_at, updated_at, synced_at, subscriber_name, phone, whatsapp_consent FROM {$table}"; // phpcs:ignore
        if ( $where ) {
            $sql .= ' WHERE ' . implode( ' AND ', $where );
        }
        $sql .= " ORDER BY id ASC LIMIT %d OFFSET %d";
        $params[] = $limit;
        $params[] = $offset;

        // phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared
        $rows = $wpdb->get_results( $wpdb->prepare( $sql, ...$params ), ARRAY_A );
        return $rows ?: [];
    }

    public static function mark_synced( array $ids, string $synced_at ): int {
        global $wpdb;
        if ( empty( $ids ) ) {
            return 0;
        }
        $table       = self::get_table();
        $placeholders = implode( ',', array_fill( 0, count( $ids ), '%d' ) );
        // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
        return (int) $wpdb->query(
            $wpdb->prepare(
                "UPDATE {$table} SET synced_at = %s WHERE id IN ({$placeholders})", // phpcs:ignore
                array_merge( [ $synced_at ], $ids )
            )
        );
    }
}
